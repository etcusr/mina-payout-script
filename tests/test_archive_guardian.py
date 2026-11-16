"""Historical gaps must heal without weakening archive or import checks."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import io

MODULE=Path(__file__).resolve().parents[1]/'archive/scripts/blocks_guardian.py'
g=None
if MODULE.exists():
    spec=importlib.util.spec_from_file_location('archive_guardian',MODULE)
    g=importlib.util.module_from_spec(spec); sys.modules[spec.name]=g; spec.loader.exec_module(g)


def block_hash(height):
    return '3N'+'1'*45+('123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'[height%58])*5


def payload(height,**changes):
    value={'version':4,'data':{'protocol_state':{'previous_state_hash':block_hash(height-1),
        'body':{'consensus_state':{'blockchain_length':str(height),'global_slot_since_genesis':str(height*2),
        'curr_global_slot_since_hard_fork':{'slot_number':str(height*2)}}}},
        'staged_ledger_diff':{'opaque':'binary'}}}
    value.update(changes)
    return json.dumps(value).encode()


class Fixture:
    def __init__(self):
        self.db=sqlite3.connect(':memory:'); self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE blocks(id INTEGER PRIMARY KEY,height INTEGER,state_hash TEXT UNIQUE,parent_hash TEXT,parent_id INTEGER,chain_status TEXT,protocol_version_id INTEGER,global_slot_since_hard_fork INTEGER,global_slot_since_genesis INTEGER)')
        for h,status in ((1,'canonical'),(2,'pending'),(8,'canonical'),(9,'pending'),(10,'pending')): self.insert(h,status)
        self.requests=[]; self.imported=[]; self.unavailable=set(); self.reject=False; self.wrong_hash=False
    def insert(self,height,status='pending'):
        self.db.execute('INSERT INTO blocks VALUES (?,?,?,?,?,?,3,?,?)',(height,height,block_hash(height),block_hash(height-1),None,status,height*2,height*2))
        self.db.execute('UPDATE blocks SET parent_id=(SELECT p.id FROM blocks p WHERE p.state_hash=blocks.parent_hash)')
    def query(self,sql): return [dict(r) for r in self.db.execute(sql)]
    def fetch(self,ref,byte_limit,timeout):
        self.requests.append(ref.height)
        if ref.height in self.unavailable: raise g.BlockUnavailable('download unavailable')
        return payload(ref.height)
    def import_blocks(self,blocks,timeout):
        if self.reject: return set()
        accepted=set()
        for block in blocks:
            if self.wrong_hash: continue
            self.insert(block.ref.height); self.imported.append(block.ref.height); accepted.add(block.ref.state_hash)
            self.db.execute('UPDATE blocks SET global_slot_since_hard_fork=?,global_slot_since_genesis=? WHERE id=?',(block.slot,block.global_slot,block.ref.height))
        current=self.db.execute("SELECT * FROM blocks WHERE chain_status='canonical' ORDER BY height DESC LIMIT 1").fetchone()
        while current:
            self.db.execute("UPDATE blocks SET chain_status='canonical' WHERE id=?",(current['id'],))
            current=self.db.execute('SELECT * FROM blocks WHERE id=?',(current['parent_id'],)).fetchone()
        return accepted


class GuardianTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(g,'historical guardian is not implemented')
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fixture=Fixture(); self.addCleanup(self.fixture.db.close)
        self.archive=g.SqlArchive(self.fixture.query)
        self.path=Path(self.tmp.name)/'retry.json'; self.now=1000
    def run_guardian(self,limit=250,check=False):
        state=g.RetryState(self.path,'fixture')
        worker=g.Guardian(self.archive,self.fixture.fetch,self.fixture.import_blocks,state,
                          g.Limits(max_blocks=limit),wall_clock=lambda:self.now)
        return worker.run([g.BlockRef(10,block_hash(10)),g.BlockRef(9,block_hash(9))],check=check)

    def test_old_gap_outside_daemon_window_heals_in_parent_first_order(self):
        result=self.run_guardian()
        self.assertEqual(self.fixture.imported,[3,4,5,6,7])
        self.assertEqual(result['imported'],5)
        self.assertEqual(result['audit']['missing_parents'],0)
        self.assertEqual(result['audit']['canonical_gaps'],0)
        self.assertEqual(result['audit']['broken_links'],0)
        self.run_guardian()
        self.assertEqual(self.fixture.imported,[3,4,5,6,7])

    def test_large_gap_progresses_across_bounded_iterations(self):
        first=self.run_guardian(limit=2)
        self.assertEqual(first['imported'],2); self.assertTrue(first['limited'])
        self.assertEqual(self.fixture.imported,[6,7])
        self.run_guardian(limit=2); self.run_guardian(limit=2)
        self.assertEqual(sorted(self.fixture.imported),[3,4,5,6,7])
        self.assertEqual(self.archive.audit()['missing_parents'],0)

    def test_download_time_budget_reserves_time_to_import_progress(self):
        tick=[0]
        def fetch(ref,byte_limit,timeout):
            tick[0]+=2
            return payload(ref.height)
        worker=g.Guardian(self.archive,fetch,self.fixture.import_blocks,
            g.RetryState(self.path,'fixture'),g.Limits(max_seconds=4),clock=lambda:tick[0])
        result=worker.run([])
        self.assertGreater(result['imported'],0)
        self.assertTrue(result['limited'])

    def test_recent_missing_count_is_rechecked_after_import(self):
        worker=g.Guardian(self.archive,self.fixture.fetch,self.fixture.import_blocks,
                          g.RetryState(self.path,'fixture'))
        result=worker.run([g.BlockRef(11,block_hash(11))])
        self.assertEqual(result['recent_missing'],0)
        self.assertIn(11,self.fixture.imported)

    def test_missing_cdn_block_is_retried_after_cooldown_not_blacklisted_forever(self):
        self.fixture.unavailable.add(7)
        result=self.run_guardian(); self.assertEqual(result['failed'],1)
        self.assertEqual(self.fixture.requests,[7])
        self.run_guardian(); self.assertEqual(self.fixture.requests,[7])
        self.fixture.unavailable.clear(); self.now+=601
        self.run_guardian(); self.assertEqual(self.fixture.imported,[3,4,5,6,7])

    def test_db_failure_does_not_trigger_download_or_import(self):
        def broken(sql): raise g.GuardianError('database unavailable')
        self.archive=g.SqlArchive(broken)
        with self.assertRaises(g.GuardianError): self.run_guardian()
        self.assertEqual(self.fixture.requests,[]); self.assertEqual(self.fixture.imported,[])

    def test_import_failure_is_not_reported_as_success(self):
        self.fixture.reject=True
        result=self.run_guardian()
        self.assertEqual(result['imported'],0); self.assertGreater(result['failed'],0)
        self.assertEqual(result['audit']['missing_parents'],1)

    def test_success_receipt_without_exact_db_row_is_rejected(self):
        self.fixture.import_blocks=lambda blocks,timeout:{b.ref.state_hash for b in blocks}
        result=self.run_guardian()
        self.assertEqual(result['imported'],0); self.assertGreater(result['failed'],0)

    def test_check_only_reports_old_gap_without_import_or_retry_writes(self):
        result=self.run_guardian(check=True)
        self.assertEqual(result['audit']['missing_parents'],1)
        self.assertEqual(result['audit']['canonical_gaps'],6)
        self.assertEqual(self.fixture.requests,[]); self.assertFalse(self.path.exists())

    def test_binary_proof_bytes_are_preserved_and_wrong_height_rejected(self):
        raw=payload(7).replace(b'binary',b'\xff')
        block=g.parse_block(raw,g.BlockRef(7,block_hash(7)))
        self.assertEqual(block.payload,raw); self.assertEqual(block.parent_hash,block_hash(6))
        with self.assertRaises(g.BlockUnavailable): g.parse_block(raw,g.BlockRef(6,block_hash(6)))

    def test_non_decreasing_slots_block_import(self):
        self.fixture.fetch=lambda ref,byte_limit,timeout:payload(ref.height).replace(
            ('"global_slot_since_genesis": "'+str(ref.height*2)+'"').encode(),b'"global_slot_since_genesis": "999"')
        result=self.run_guardian()
        self.assertEqual(result['imported'],0); self.assertGreater(result['failed'],0)

    def test_hard_fork_slot_reset_uses_monotonic_genesis_slot(self):
        def fetch(ref,byte_limit,timeout):
            value=json.loads(payload(ref.height))
            value['data']['protocol_state']['body']['consensus_state']['curr_global_slot_since_hard_fork']['slot_number']=str(ref.height-3)
            return json.dumps(value).encode()
        self.fixture.fetch=fetch
        self.assertEqual(self.run_guardian()['imported'],5)

    def test_importer_exit_zero_still_requires_success_receipt(self):
        block=g.parse_block(payload(7),g.BlockRef(7,block_hash(7)))
        with patch.object(g.subprocess,'run',return_value=SimpleNamespace(returncode=0)):
            self.assertEqual(g.MinaImporter('fixture')([block],30),set())

    def test_importer_failure_receipt_overrides_success_and_bytes_are_unchanged(self):
        raw=payload(7).replace(b'binary',b'\xff')
        block=g.parse_block(raw,g.BlockRef(7,block_hash(7)))
        def run(args,**kwargs):
            self.assertEqual(Path(args[-1]).read_bytes(),raw)
            for flag in ('--successful-files','--failed-files'):
                Path(args[args.index(flag)+1]).write_text(args[-1]+'\n')
            return SimpleNamespace(returncode=0)
        with patch.object(g.subprocess,'run',side_effect=run):
            self.assertEqual(g.MinaImporter('fixture')([block],30),set())

    def test_http_invalid_primary_uses_mirror_without_changing_payload(self):
        def response(raw):
            value=io.BytesIO(raw); value.headers={}; return value
        raw=payload(7).replace(b'binary',b'\xff')
        with patch.object(g.urllib.request,'urlopen',side_effect=[response(b'bad json'),response(raw)]) as opened:
            fetched=g.HttpFetcher()(g.BlockRef(7,block_hash(7)),1024*1024,30)
        self.assertEqual(fetched,raw); self.assertEqual(opened.call_count,2)

    def test_http_byte_limit_is_enforced_without_content_length(self):
        def response(*args,**kwargs):
            value=io.BytesIO(b'x'*100); value.headers={}; return value
        with patch.object(g.urllib.request,'urlopen',side_effect=response):
            with self.assertRaises(g.BlockUnavailable):
                g.HttpFetcher()(g.BlockRef(7,block_hash(7)),10,30)

    def test_invalid_downloads_also_consume_iteration_byte_budget(self):
        class Fetcher:
            bytes_received=0
            def __call__(self,ref,byte_limit,timeout):
                self.bytes_received+=byte_limit
                raise g.BlockUnavailable('invalid payload')
        fetch=Fetcher()
        worker=g.Guardian(self.archive,fetch,self.fixture.import_blocks,
            g.RetryState(self.path,'fixture'),g.Limits(max_bytes=10))
        worker.run([g.BlockRef(11,block_hash(11))])
        self.assertEqual(fetch.bytes_received,10)

    def test_corrupted_retry_state_stops_before_archive_mutation(self):
        self.path.write_text('{broken')
        with self.assertRaises(g.GuardianError): self.run_guardian()
        self.assertEqual(self.fixture.imported,[])

    def test_concurrent_iteration_cannot_acquire_same_lock(self):
        with g.iteration_lock(Path(self.tmp.name)):
            with self.assertRaises(g.AlreadyRunning):
                with g.iteration_lock(Path(self.tmp.name)): pass
