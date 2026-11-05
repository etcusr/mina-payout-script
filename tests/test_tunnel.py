"""No SSH or external service is contacted by these tests."""
import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import tunnel

CONFIG = {'SSH_HOST': 'test-node', 'REMOTE_GRAPHQL_PORT': 6521,
          'GRAPHQL_PORT': 3085, 'ARCHIVE_DB_URL': 'postgresql://u:p@127.0.0.1:5432/archive'}


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=True); self.environment.start(); self.addCleanup(self.environment.stop)
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.health = {'graphql': 'healthy', 'postgres': 'healthy'}; self.commands = []
        self.guard = threading.Lock()
    def manager(self):
        def probe(config, endpoint): return self.health[endpoint]
        def run(command):
            with self.guard:
                self.commands.append(command)
                if '-O' in command and command[command.index('-O')+1]!='forward': return 0
                time.sleep(.02)
                for i, value in enumerate(command):
                    if value=='-L':
                        if command[i+1].startswith('127.0.0.1:3085:'): self.health['graphql']='healthy'
                        if command[i+1].startswith('127.0.0.1:5432:'): self.health['postgres']='healthy'
                return 0
        return tunnel.Tunnel(CONFIG, state_dir=self.tmp.name, probe=probe, run=run)
    def test_healthy_endpoints_reused_without_starting_ssh(self):
        self.assertFalse(self.manager().ensure()['created']); self.assertEqual(self.commands, [])
    def test_missing_endpoints_start_background_batchmode_owned_master(self):
        self.health = dict.fromkeys(self.health, 'missing')
        got = self.manager().ensure()
        self.assertTrue(got['created'])
        cmd = self.commands[0]
        self.assertIn('BatchMode=yes', cmd); self.assertIn('ExitOnForwardFailure=yes', cmd)
        self.assertIn('127.0.0.1:3085:127.0.0.1:6521', cmd)
        self.assertIn('127.0.0.1:5432:127.0.0.1:5432', cmd)
        self.assertTrue(Path(got['control_path']).with_suffix('.owner.json').exists())
    def test_partial_tunnel_only_adds_missing_forward(self):
        self.health['postgres'] = 'missing'
        self.manager().ensure()
        cmd=self.commands[0]
        self.assertNotIn('127.0.0.1:3085:127.0.0.1:6521', cmd)
        self.assertIn('127.0.0.1:5432:127.0.0.1:5432', cmd)
    def test_occupied_wrong_service_is_never_replaced(self):
        self.health['postgres']='occupied'
        with self.assertRaises(tunnel.TunnelError): self.manager().ensure()
        self.assertEqual(self.commands, [])
    def test_concurrent_ensure_creates_one_connection(self):
        self.health=dict.fromkeys(self.health, 'missing')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            got=list(executor.map(lambda _: self.manager().ensure(), range(2)))
        self.assertEqual(sum(x['created'] for x in got), 1)
        self.assertEqual(len(self.commands), 1)
    def test_stop_without_ownership_never_uses_pid_or_ssh(self):
        self.assertFalse(self.manager().stop()['stopped']); self.assertEqual(self.commands, [])
    def test_owned_stop_uses_control_exit(self):
        self.health['postgres']='missing'; manager=self.manager(); manager.ensure(); manager.stop()
        self.assertEqual(self.commands[-1][-3:], ['-O', 'exit', 'test-node'])
    def test_later_missing_forward_is_added_to_owned_master(self):
        self.health['postgres']='missing'; manager=self.manager(); manager.ensure()
        self.health['graphql']='missing'; got=manager.ensure()
        self.assertTrue(got['created'])
        self.assertIn('forward',self.commands[-1]); self.assertNotIn('-M',self.commands[-1])
        self.assertIn('127.0.0.1:3085:127.0.0.1:6521',self.commands[-1])
        self.assertNotIn('127.0.0.1:5432:127.0.0.1:5432',self.commands[-1])
    def test_ssh_failure_never_claims_ownership(self):
        manager=self.manager(); self.health['postgres']='missing'; manager.run=lambda command: 1
        with self.assertRaises(tunnel.TunnelError): manager.ensure()
        self.assertFalse(manager.owner.exists())
    def test_stale_or_foreign_control_socket_is_never_reused(self):
        manager=self.manager(); manager.control_path.write_text('unrelated')
        self.health['postgres']='missing'
        with self.assertRaises(tunnel.TunnelError): manager.ensure()
        self.assertEqual(self.commands,[])
    def test_control_path_fits_macos_socket_limit(self):
        with patch.object(tunnel.tempfile, 'gettempdir', return_value='/'+'x'*200):
            manager=tunnel.Tunnel(CONFIG)
        self.assertLess(len(str(manager.control_path).encode()),104)
    def test_postgres_probe_connects_to_the_forwarded_port(self):
        import types
        captured={}
        class Connection:
            def cursor(self): return self
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def execute(self,query): pass
            def fetchone(self): return (True,)
            def close(self): pass
        def connect(dsn,**kwargs): captured.update(kwargs); return Connection()
        cfg=tunnel._settings(dict(CONFIG,PG_PORT=9999))
        with patch.dict('sys.modules',{'psycopg2':types.SimpleNamespace(connect=connect)}), patch.object(tunnel.socket,'create_connection',return_value=Connection()):
            self.assertEqual(tunnel._endpoint_state(cfg,'postgres'),'healthy')
        self.assertEqual(captured.get('port'),9999)
        self.assertEqual(captured.get('host'),'127.0.0.1')
    def test_loader_normalizes_environment_ports_for_all_consumers(self):
        path=Path(self.tmp.name)/'config.json'; path.write_text(json.dumps(CONFIG))
        with patch.dict(os.environ,{'PG_PORT':'9999','GRAPHQL_PORT':'8888'}):
            cfg=tunnel.load_config(path)
            again=tunnel._settings(cfg)
        self.assertEqual(cfg['GRAPHQL_PORT'],8888)
        self.assertEqual(cfg['PG_PORT'],9999)
        self.assertEqual(cfg['ARCHIVE_DB_URL'],'postgresql://u:p@127.0.0.1:9999/archive')
        self.assertEqual(again,cfg)
    def test_query_endpoint_overrides_cannot_diverge_from_effective_config(self):
        from psycopg2.extensions import parse_dsn
        cfg=dict(CONFIG,ARCHIVE_DB_URL='postgresql://u:p@127.0.0.1:5432/archive?host=127.0.0.2&port=6543&application_name=mina%20pool')
        with patch.dict(os.environ,{'PG_PORT':'9999'}): effective=tunnel._settings(cfg)
        parsed=parse_dsn(effective['ARCHIVE_DB_URL'])
        self.assertEqual((effective['PG_HOST'],effective['PG_PORT']),('127.0.0.2',9999))
        self.assertEqual((parsed['host'],parsed['port']),('127.0.0.2','9999'))
        self.assertEqual(parsed['application_name'],'mina pool')
        self.assertEqual(parsed['password'],'p')
    def test_socket_multihost_hostaddr_and_service_endpoints_fail_actionably(self):
        for url in ('postgresql://u:p@localhost/db?hostaddr=127.0.0.2',
                    'postgresql://u:p@localhost/db?service=archive',
                    'postgresql://u:p@localhost/db?host=/tmp',
                    'postgresql://u:p@localhost/db?host=localhost,other',
                    'postgresql://u:p@localhost/db?host=other%40localhost',
                    'postgresql://u:p@localhost/db?host=localhost%23other'):
            with self.subTest(url=url):
                with self.assertRaises(tunnel.TunnelError): tunnel._settings(dict(CONFIG,ARCHIVE_DB_URL=url))
    def test_configured_remote_port_and_environment_host(self):
        with patch.dict(os.environ, {'MINA_SSH_HOST':'env-node', 'REMOTE_GRAPHQL_PORT':'7777'}):
            self.health['graphql']='missing'; self.manager().ensure()
        self.assertEqual(self.commands[0][-1], 'env-node')
        self.assertIn('127.0.0.1:3085:127.0.0.1:7777', self.commands[0])
    def test_missing_remote_graphql_config_fails_before_start(self):
        self.health['graphql']='missing'; cfg=dict(CONFIG); del cfg['REMOTE_GRAPHQL_PORT']
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(tunnel.TunnelError):
                tunnel.Tunnel(cfg, state_dir=self.tmp.name, probe=lambda c,e:self.health[e], run=lambda c: self.commands.append(c)).ensure()
        self.assertEqual(self.commands, [])

if __name__=='__main__': unittest.main()
