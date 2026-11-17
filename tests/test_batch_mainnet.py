import json
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from batch_accounting import BatchStore, canonical_plan
from payout_state import StateError
from tests.test_batch_accounting import fixture
from batch_mainnet import BatchWorkflow, signer_packet, submit_once, read_password


class ReadonlyOrdinaryJournalTests(unittest.TestCase):
    def test_archive_ingest_lag_retries_only_proof_and_bounds_retries(self):
        from batch_mainnet import MainnetAdapter
        from preflight import PreflightArchiveLagError
        adapter=MainnetAdapter.__new__(MainnetAdapter);adapter.config={}
        expected={'tx_hash':'expected'};proof={'status':'applied','depth':3}
        with patch('batch_archive.verify',side_effect=[PreflightArchiveLagError('lag'),proof]) as verify,\
             patch('batch_mainnet.time.sleep') as sleep,patch('builtins.print'),\
             patch.object(adapter,'submit') as submit:
            self.assertEqual(adapter.proof(expected),proof)
            self.assertEqual(verify.call_count,2);sleep.assert_called_once_with(15);submit.assert_not_called()
        with patch('batch_archive.verify',side_effect=PreflightArchiveLagError('lag')) as verify,\
             patch('batch_mainnet.time.sleep') as sleep,patch('builtins.print'):
            with self.assertRaises(PreflightArchiveLagError):adapter.proof(expected)
            self.assertEqual(verify.call_count,3);self.assertEqual(sleep.call_count,2)

    def test_archive_identity_error_does_not_retry(self):
        from batch_mainnet import MainnetAdapter
        from batch_archive import BatchArchiveError
        adapter=MainnetAdapter.__new__(MainnetAdapter);adapter.config={}
        with patch('batch_archive.verify',side_effect=BatchArchiveError('identity')) as verify,\
             patch('batch_mainnet.time.sleep') as sleep:
            with self.assertRaises(BatchArchiveError):adapter.proof({})
            verify.assert_called_once();sleep.assert_not_called()

    def test_readonly_neither_migrates_old_schema_nor_allows_writes(self):
        from payout_state import StateStore
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            original=StateStore(directory,create=True)
            before=original.path.read_bytes()
            reader=StateStore(directory,readonly=True)
            with reader._connection() as db:
                with self.assertRaises(sqlite3.OperationalError):db.execute('UPDATE metadata SET version=2')
            self.assertEqual(before,original.path.read_bytes())
            with original._connection() as db:db.execute('UPDATE metadata SET version=2')
            old=original.path.read_bytes()
            with self.assertRaisesRegex(StateError,'upgrade|version|migration'):
                StateStore(directory,readonly=True)
            self.assertEqual(old,original.path.read_bytes())

    def test_adapter_reads_nested_legacy_helpers_without_writes_and_caps_budget(self):
        import owner_income
        from payout_state import StateStore
        from payout_workflow import Workflow
        from batch_mainnet import MainnetAdapter
        from tests.test_workflow import Fake
        plan=canonical_plan(fixture())
        cfg={'SEND_FROM_ADDRESS':plan['wallet'],'EXPECTED_CHAIN_ID':'chain'}
        fake=Fake()
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);legacy=Workflow(cfg,root,adapter=fake);legacy.initialize()
            store=legacy.store()
            owner_income.initialize_account(store,plan['wallet'],'chain',fake.nonce)
            owner_income.record_credit(store,{'wallet':plan['wallet'],'network':'chain','era':'3','epoch':9,
                'validator_nano':100,'retained_own_nano':0,'payout_fees_nano':0,'own_paid_nano':0,
                'own_retained_by_address':{},'source':{'kind':'test'}})
            adapter=MainnetAdapter.__new__(MainnetAdapter)
            adapter.config=cfg;adapter.root=root;adapter.state_dir=root/'.payout-state'
            adapter.legacy=Workflow(cfg,root,adapter=fake,readonly=True)
            adapter.health=fake.health
            before=store.path.read_bytes();real_connect=StateStore._connection
            def read_only_connection(opened):
                self.assertTrue(opened.readonly)
                return real_connect(opened)
            with patch.object(StateStore,'_connection',read_only_connection):
                self.assertEqual(adapter.inspect(plan)['batch_budget']['available_nano'],100)
            self.assertEqual(before,store.path.read_bytes())
            borrowed=canonical_plan(fixture(reserve_advance_nano=20))
            fake.liquid=300
            with patch.object(adapter.legacy,'carry',return_value={'delegator':300}):
                budget=adapter.inspect(borrowed)['batch_budget']
                self.assertEqual((budget['available_nano'],budget['shortfall_nano'],budget['projected_carry_shortfall_nano']),(20,0,20))
                self.assertEqual(adapter.inspect(plan)['batch_budget']['available_nano'],0)
                fake.liquid=299
                self.assertEqual(adapter.inspect(borrowed)['batch_budget']['shortfall_nano'],1)
                fake.liquid=320
                self.assertEqual(adapter.inspect(borrowed)['batch_budget']['projected_carry_shortfall_nano'],0)
            self.assertEqual(before,store.path.read_bytes())
            batches=BatchStore(adapter.state_dir);digest=batches.create_plan(plan)
            fake.liquid=320
            with patch.object(adapter.legacy,'carry',return_value={'delegator':300}):
                budget=adapter.inspect(plan,digest)['batch_budget']
                self.assertEqual((budget['needed_nano'],budget['available_nano'],budget['shortfall_nano']),(20,20,0))
                fake.liquid=300
                budget=adapter.inspect(plan,digest)['batch_budget']
                self.assertEqual((budget['available_nano'],budget['shortfall_nano']),(0,20))


class FakeAdapter:
    def __init__(self):
        self.nonce=0; self.calls=[]; self.proofs={}; self.loss=None; self.balance=1000; self.network='chain'
    def approve(self,plan,digest=None):
        self.calls.append('approve')
        if self.balance<sum(r['amount_nano'] for r in plan['recipients'])+plan['fee_nano']*len(plan['batches']):
            raise StateError('reserved funds are protected')
        return {'network':self.network,'era':3,'account':{'nonce':self.nonce,'inferred_nonce':self.nonce}}
    def health(self): return {'network':self.network,'era':3,'account':{'nonce':self.nonce}}
    def build(self,expected): self.calls.append('build'); return json.dumps(expected)
    def sign(self,unsigned,expected,password):
        self.calls.append('sign');return {'signed':json.dumps({'expected':expected}),'tx_hash':'tx'+str(expected['nonce'])}
    def validate(self,signed,expected):
        self.calls.append('validate')
        return {'tx_hash':'tx'+str(expected['nonce']),'query':'mutation'}
    def submit(self,signed,expected):
        self.calls.append('submit')
        if self.loss=='before': raise ConnectionError('lost')
        self.nonce+=1
        self.proofs[expected['tx_hash']]={**expected,'status':'applied','depth':15,'height':10,'block_hash':'block','on_best_chain':True}
        if self.loss=='after':raise ConnectionError('lost')
        return expected['tx_hash']
    def proof(self,expected,required_depth=3):
        return self.proofs.get(expected['tx_hash'],{'status':'unknown'})


class BatchMainnetTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.store=BatchStore(self.root);self.digest=self.store.create_plan(fixture())
        self.fake=FakeAdapter();self.engine=BatchWorkflow(self.store,self.fake)
    def send(self):return self.engine.send_next(self.digest,lambda:'test-password')
    def test_single_batch_claim_before_actual_submit_and_restart_after_lost_response(self):
        self.fake.loss='after'
        with self.assertRaises(ConnectionError):self.send()
        self.assertEqual(self.store.rows(self.digest)[0]['status'],'unknown')
        self.engine=BatchWorkflow(BatchStore(self.root,create=False),self.fake)
        self.send()
        self.assertEqual(self.fake.calls.count('submit'),1)
        self.assertEqual([r['status'] for r in self.store.rows(self.digest)],['confirmed','planned'])
    def test_lost_before_acceptance_is_not_resubmitted(self):
        self.fake.loss='before'
        with self.assertRaises(ConnectionError):self.send()
        self.send();self.assertEqual(self.fake.calls.count('submit'),1)
        self.assertEqual(self.store.snapshot(fixture()['wallet'],'chain')['reserved_nano'],20)
    def test_serial_split_has_one_nonce_and_fee_per_batch(self):
        self.send();self.engine.reconcile(self.digest);self.send();self.engine.reconcile(self.digest)
        self.send()
        self.assertEqual(self.fake.calls.count('submit'),2)
        self.assertEqual([r['nonce'] for r in self.store.rows(self.digest)],[0,1])
        self.assertEqual(self.store.snapshot(fixture()['wallet'],'chain')['spent_nano'],20)
    def test_failed_in_chain_charges_only_fee_and_stops_next_batch(self):
        self.send();self.fake.proofs['tx0']['status']='failed'
        self.engine.reconcile(self.digest)
        with self.assertRaisesRegex(StateError,'failed|не выполнен'):self.send()
        self.assertEqual(self.fake.calls.count('submit'),1)
        self.assertEqual(self.store.snapshot(fixture()['wallet'],'chain')['spent_nano'],7)
        self.assertEqual(self.store.snapshot(fixture()['wallet'],'chain')['reserved_nano'],0)
        self.assertEqual(self.store.rows(self.digest)[1]['status'],'cancelled')
        self.assertEqual(self.store.snapshot(fixture()['wallet'],'chain')['unfinished'],[])
    def test_budget_drop_after_signing_does_not_claim_or_submit(self):
        sign=self.fake.sign
        def change(*args):
            result=sign(*args);self.fake.balance=0;return result
        self.fake.sign=change
        with self.assertRaises(StateError):self.send()
        self.assertEqual(self.store.rows(self.digest)[0]['status'],'prepared')
        self.assertNotIn('submit',self.fake.calls)
    def test_stale_nonce_after_prepared_restart_cannot_resign(self):
        plan=self.store.get_plan(self.digest);expected=signer_packet(plan,0,0)
        signed=self.fake.sign('',expected,'x');self.store.prepare(self.digest,0,0,signed['signed'],signed['tx_hash'])
        self.fake.nonce=1
        with self.assertRaisesRegex(StateError,'nonce|Nonce'):self.send()
        self.assertEqual(self.fake.calls.count('sign'),1)
        self.assertNotIn('submit',self.fake.calls)
    def test_wrong_network_stops_before_signing(self):
        self.fake.network='other'
        with self.assertRaises(StateError):self.send()
        self.assertNotIn('sign',self.fake.calls)
    def test_historical_proof_survives_protocol_upgrade_but_new_signing_stops(self):
        self.send();self.engine.reconcile(self.digest)
        self.fake.health=lambda:{'network':'chain','era':4}
        self.engine.reconcile(self.digest)
        self.fake.approve=lambda *args:{'network':'chain','era':4,'account':{'nonce':1}}
        with self.assertRaisesRegex(StateError,'era'):self.send()
        self.assertEqual(self.fake.calls.count('sign'),1)

    def test_no_visible_password_fallback(self):
        import warnings, getpass
        def fallback(*args):warnings.warn('no tty',getpass.GetPassWarning)
        with patch('batch_mainnet.getpass.getpass',side_effect=fallback):
            with self.assertRaisesRegex(StateError,'терминале'):read_password()

    def test_large_recipient_set_splits_without_loss_or_extra_fee(self):
        from tests.test_calculation import address
        recipients=[{'recipient':address(i),'amount_nano':1} for i in range(10,75)]
        plan=canonical_plan(fixture(recipients=recipients,batch_size=30,fee_nano=6000001))
        self.assertEqual([len(b['recipients']) for b in plan['batches']],[30,30,5])
        self.assertEqual([r for b in plan['batches'] for r in b['recipients']],recipients)
        self.assertEqual(sum(sum(r['amount_nano'] for r in b['recipients'])+plan['fee_nano'] for b in plan['batches']),18000068)
    def test_reorg_of_confirmed_batch_blocks_later_send(self):
        self.send();self.engine.reconcile(self.digest);self.fake.proofs.clear()
        with self.assertRaises(StateError):self.send()
        self.assertEqual(self.fake.calls.count('submit'),1)
    def test_reorg_while_entering_password_blocks_later_send(self):
        self.send();self.engine.reconcile(self.digest)
        sign=self.fake.sign
        def reorg(*args):
            result=sign(*args);self.fake.proofs.clear();return result
        self.fake.sign=reorg
        with self.assertRaises(StateError):self.send()
        self.assertEqual(self.fake.calls.count('submit'),1)
    def test_transport_error_is_one_attempt_and_does_not_expose_raw_error(self):
        with patch('batch_mainnet.urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect=OSError('SECRET')
            with self.assertRaisesRegex(StateError,'неизвестен') as error:
                submit_once({'GRAPHQL_HOST':'127.0.0.1','GRAPHQL_PORT':3085},'mutation','hash')
            self.assertNotIn('SECRET',str(error.exception));self.assertEqual(opener.return_value.open.call_count,1)

    def test_help_is_available_without_wallet_or_network(self):
        result=subprocess.run([__import__('sys').executable,'batch_mainnet.py','--help'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0)
        self.assertIn('preview',result.stdout);self.assertIn('resume',result.stdout)

    def test_declining_cli_review_does_not_create_financial_journal_or_sign(self):
        from batch_mainnet import main
        from batch_accounting import FILENAME
        self.fake.config={'SEND_FROM_ADDRESS':fixture()['wallet'],'EXPECTED_CHAIN_ID':'chain'}
        self.fake.inspect=lambda p:{**self.fake.health(),'batch_budget':{}}
        proposal=self.root/'proposal.json';proposal.write_text(json.dumps(fixture()))
        with patch('batch_mainnet.PROJECT',self.root),patch('tunnel.load_config',return_value=self.fake.config),\
             patch('tunnel.ensure'),patch('batch_mainnet.MainnetAdapter',return_value=self.fake),\
             patch('batch_mainnet.show_plan'),patch('builtins.input',return_value='n'),patch('builtins.print'):
            self.assertEqual(main(['send','--proposal',str(proposal)]),0)
        self.assertFalse((self.root/'.payout-state'/FILENAME).exists())
        self.assertNotIn('sign',self.fake.calls);self.assertNotIn('submit',self.fake.calls)

    def test_cli_reserve_permission_persists_and_prints_remaining_coverage(self):
        import io,contextlib
        from batch_mainnet import main
        self.fake.config={'SEND_FROM_ADDRESS':fixture()['wallet'],'EXPECTED_CHAIN_ID':'chain'}
        self.fake.inspect=lambda p,d=None:{**self.fake.health(),'batch_budget':{
            'liquid_nano':280,'carry_nano':300,'projected_carry_shortfall_nano':20}}
        proposal=self.root/'borrow.json';proposal.write_text(json.dumps(fixture(reserve_advance_nano=20)))
        output=io.StringIO()
        with patch('batch_mainnet.PROJECT',self.root),patch('tunnel.load_config',return_value=self.fake.config),\
             patch('tunnel.ensure'),patch('batch_mainnet.MainnetAdapter',return_value=self.fake),\
             patch('batch_mainnet.show_plan'),patch('builtins.input',return_value='y'),\
             patch('batch_mainnet.read_password',return_value='test'),contextlib.redirect_stdout(output):
            self.assertEqual(main(['send','--proposal',str(proposal)]),0)
        store=BatchStore(self.root/'.payout-state',create=False)
        digest,plan=store.list_plans()[0]
        self.assertEqual(plan['reserve_advance_nano'],20)
        self.assertTrue(all(r['status']=='confirmed' for r in store.rows(digest)))
        self.assertIn('Нужно пополнить для покрытия',output.getvalue())
        self.assertIn('0.000000020',output.getvalue())
