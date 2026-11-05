"""Serialized SSH forwarding with service checks and owned control sockets."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile
import time
from urllib.parse import parse_qsl, unquote, unquote_plus, urlparse, urlunparse

from preflight import _graphql


class TunnelError(RuntimeError):
    pass


def _port(value):
    try: port = int(value)
    except (ValueError, TypeError): raise TunnelError('Tunnel ports must be integers') from None
    if not 1 <= port <= 65535: raise TunnelError('Tunnel ports must be in 1..65535')
    return port


def _settings(config):
    cfg = dict(config)
    for key in ('GRAPHQL_HOST', 'GRAPHQL_PORT', 'PG_PORT', 'REMOTE_GRAPHQL_PORT', 'REMOTE_PG_PORT'):
        if os.environ.get(key): cfg[key] = os.environ[key]
    cfg['SSH_HOST'] = os.environ.get('MINA_SSH_HOST') or cfg.get('SSH_HOST', '')
    try:
        parsed = urlparse(cfg.get('ARCHIVE_DB_URL', ''))
        if parsed.scheme not in ('postgresql', 'postgres') or not parsed.netloc or parsed.fragment:
            raise TunnelError('ARCHIVE_DB_URL must be a single-host TCP PostgreSQL URI')
        parameters = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        overrides = {}
        for key, value in parameters:
            if key in ('hostaddr', 'service', 'servicefile'):
                raise TunnelError('ARCHIVE_DB_URL hostaddr/service overrides are unsupported; use one explicit TCP host and port')
            if key in ('host', 'port'):
                if key in overrides: raise TunnelError('ARCHIVE_DB_URL has duplicate endpoint overrides; use one TCP host and port')
                overrides[key] = value
        hostname = overrides.get('host', unquote(parsed.hostname or ''))
        if not hostname or any(char in hostname for char in ',/\\@?#%[]') or any(char.isspace() or ord(char)<32 for char in hostname):
            raise TunnelError('ARCHIVE_DB_URL must use one TCP hostname; socket paths and multiple hosts are unsupported')
        authority_port = parsed.port
        port = cfg.get('PG_PORT', overrides.get('port', authority_port or 5432))
        cfg['PG_PORT'] = _port(port)
        cfg['PG_HOST'] = hostname
        cfg['GRAPHQL_PORT'] = _port(cfg.get('GRAPHQL_PORT', 3085))
        # Move effective libpq endpoint overrides into the authority, so probes,
        # preflight and all later consumers connect to exactly the same TCP peer.
        # Keep raw credentials and unrelated query options (including encoding).
        authority = parsed.netloc.rsplit('@', 1)
        userinfo = authority[0]+'@' if len(authority)==2 else ''
        bracketed = '['+hostname+']' if ':' in hostname else hostname
        query = '&'.join(part for part in parsed.query.split('&')
                         if unquote_plus(part.split('=',1)[0]) not in ('host', 'port'))
        cfg['ARCHIVE_DB_URL'] = urlunparse(parsed._replace(
            netloc=userinfo+bracketed+':'+str(cfg['PG_PORT']), query=query))
    except TunnelError:
        raise
    except (ValueError, TypeError):
        raise TunnelError('ARCHIVE_DB_URL must be a valid single-host TCP PostgreSQL URI') from None
    cfg['GRAPHQL_HOST'] = cfg.get('GRAPHQL_HOST', '127.0.0.1')
    return cfg


def _endpoint_state(config, endpoint):
    host = config['GRAPHQL_HOST'] if endpoint == 'graphql' else config['PG_HOST']
    port = config['GRAPHQL_PORT'] if endpoint == 'graphql' else config['PG_PORT']
    try:
        with socket.create_connection((host, port), timeout=3): pass
    except ConnectionRefusedError: return 'missing'
    except OSError: return 'occupied'  # An ambiguous endpoint must never be stolen.
    connection = None
    try:
        if endpoint == 'graphql':
            cfg = dict(config, PREFLIGHT_TIMEOUT_SECONDS=5)
            data = _graphql(cfg, '{ syncStatus daemonStatus { chainId } }')
            if not isinstance(data.get('syncStatus'), str) or not data.get('daemonStatus', {}).get('chainId'):
                return 'occupied'
            if cfg.get('EXPECTED_CHAIN_ID') and data['daemonStatus']['chainId'] != cfg['EXPECTED_CHAIN_ID']:
                return 'occupied'
        else:
            import psycopg2
            connection = psycopg2.connect(config['ARCHIVE_DB_URL'], host=config['PG_HOST'],
                                         port=config['PG_PORT'], connect_timeout=5,
                                         options='-c default_transaction_read_only=on -c statement_timeout=5000')
            with connection.cursor() as cursor:
                cursor.execute('SELECT EXISTS(SELECT 1 FROM blocks LIMIT 1)')
                if cursor.fetchone()[0] is not True: return 'occupied'
        return 'healthy'
    except Exception:
        return 'occupied'
    finally:
        if connection is not None: connection.close()


def _run(command):
    try:
        return subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


class Tunnel:
    def __init__(self, config, *, state_dir=None, probe=None, run=None):
        self.config = _settings(config)
        identity = {key: self.config.get(key) for key in ('SSH_HOST','GRAPHQL_HOST','GRAPHQL_PORT','PG_HOST','PG_PORT','REMOTE_GRAPHQL_PORT','REMOTE_PG_PORT')}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]
        self.directory = Path(state_dir) if state_dir else Path('/tmp') / ('mina-payout-{}-{}'.format(os.getuid(), digest))
        self.control_path = self.directory / 'control.sock'
        self.owner = self.control_path.with_suffix('.owner.json')
        self.probe = probe or _endpoint_state
        self.run = run or _run

    @contextmanager
    def _lock(self):
        try: self.directory.mkdir(mode=0o700)
        except FileExistsError: pass
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise TunnelError('Tunnel control directory must be a private directory owned by this user')
        fd = os.open(self.directory / 'ensure.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != os.getuid(): raise TunnelError('Tunnel lock is not owned by this user')
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _owned(self):
        try:
            info = self.owner.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid(): return False
            metadata = json.loads(self.owner.read_text())
            return metadata == {'host': self.config['SSH_HOST'], 'control_path': str(self.control_path)}
        except (OSError, ValueError): return False

    def ensure(self):
        with self._lock():
            states = {endpoint: self.probe(self.config, endpoint) for endpoint in ('graphql', 'postgres')}
            for endpoint, state in states.items():
                if state not in ('healthy', 'missing'):
                    raise TunnelError('{} endpoint is occupied or unhealthy; inspect the listener and credentials; no process was stopped'.format(endpoint))
            missing = [endpoint for endpoint, state in states.items() if state=='missing']
            if not missing: return {'created': False, 'endpoints': states, 'control_path': str(self.control_path)}
            host = self.config['SSH_HOST']
            if not host: raise TunnelError('Set SSH_HOST in config.yml or export MINA_SSH_HOST to start missing forwards')
            if host.startswith('-') or any(x.isspace() for x in host): raise TunnelError('SSH_HOST must be one SSH host or configured alias')
            forwards = []
            for endpoint in missing:
                local = self.config['GRAPHQL_PORT' if endpoint=='graphql' else 'PG_PORT']
                service_host = self.config['GRAPHQL_HOST' if endpoint=='graphql' else 'PG_HOST']
                if service_host not in ('127.0.0.1', 'localhost'):
                    raise TunnelError('Automatic SSH forwarding requires loopback service hosts')
                remote_key = 'REMOTE_GRAPHQL_PORT' if endpoint=='graphql' else 'REMOTE_PG_PORT'
                remote = self.config.get(remote_key)
                if remote is None and endpoint=='graphql': raise TunnelError('Set REMOTE_GRAPHQL_PORT explicitly for the remote daemon')
                remote = _port(remote if remote is not None else local)
                forwards.extend(['-L', '127.0.0.1:{}:127.0.0.1:{}'.format(local, remote)])
            base = ['ssh', '-S', str(self.control_path)]
            existing = self._owned() and self.run(base+['-O', 'check', host])==0
            if existing:
                command = base + ['-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-O', 'forward'] + forwards + [host]
            else:
                if self.control_path.exists():
                    raise TunnelError('Tunnel control socket already exists without a live owned master; inspect it manually')
                command = base+['-M', '-f', '-N', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
                                '-o', 'ConnectTimeout=10', '-o', 'ControlPersist=no', '-o', 'ServerAliveInterval=30',
                                '-o', 'ServerAliveCountMax=3']+forwards+[host]
            if self.run(command)!=0:
                raise TunnelError('SSH forwarding failed; verify SSH alias, BatchMode authentication and remote ports')
            if not existing:
                fd = os.open(self.owner, os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd,'w') as output:
                    json.dump({'host': host, 'control_path': str(self.control_path)}, output)
            deadline = time.monotonic()+float(self.config.get('TUNNEL_READY_TIMEOUT_SECONDS', 15))
            while True:
                states = {endpoint: self.probe(self.config, endpoint) for endpoint in ('graphql', 'postgres')}
                if all(state=='healthy' for state in states.values()):
                    return {'created': True, 'endpoints': states, 'control_path': str(self.control_path)}
                if time.monotonic()>=deadline:
                    raise TunnelError('SSH started but services are not ready; check remote ports and services; owned tunnel retained for inspection')
                time.sleep(.2)

    def stop(self):
        with self._lock():
            if not self._owned(): return {'stopped': False}
            result = self.run(['ssh', '-S', str(self.control_path), '-O', 'exit', self.config['SSH_HOST']])
            if result!=0:
                raise TunnelError('Owned SSH control connection did not stop; no process IDs were signalled')
            self.owner.unlink()
            return {'stopped': True}


def ensure(config):
    return Tunnel(config).ensure()


def stop(config):
    return Tunnel(config).stop()


def load_config(path=None):
    import yaml
    path = Path(path) if path else Path(__file__).resolve().parent/'config.yml'
    try:
        from decimal import Decimal
        class ExactLoader(yaml.SafeLoader):
            pass
        ExactLoader.add_constructor('tag:yaml.org,2002:float', lambda loader,node: Decimal(loader.construct_scalar(node).replace('_','')))
        value = yaml.load(path.read_text(), Loader=ExactLoader)
        if not isinstance(value, dict): raise ValueError()
        return _settings(value)
    except TunnelError:
        raise
    except Exception:
        raise TunnelError('Cannot read valid tunnel configuration') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('host', nargs='?')
    parser.add_argument('--config')
    parser.add_argument('--ensure', action='store_true', help='ensure missing forwards in background')
    parser.add_argument('--bg', action='store_true')
    parser.add_argument('--stop', action='store_true', help='stop only this tool\'s owned SSH control connection')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.host:
            config['SSH_HOST'] = args.host
            # Explicit manual host wins over the shell default for this command.
            os.environ.pop('MINA_SSH_HOST', None)
        manager = Tunnel(config)
        result = manager.stop() if args.stop else manager.ensure()
        print(json.dumps(result, sort_keys=True))
        if not args.stop and not args.ensure and not args.bg and result['created']:
            print('Tunnel ready. Ctrl+C stops this owned connection.', flush=True)
            try:
                while True:
                    time.sleep(1)
                    if manager.run(['ssh','-S',str(manager.control_path),'-O','check',manager.config['SSH_HOST']])!=0: return 1
            except KeyboardInterrupt:
                manager.stop()
        return 0
    except TunnelError as error:
        print(str(error), file=__import__('sys').stderr)
        return 1


if __name__ == '__main__': raise SystemExit(main())
