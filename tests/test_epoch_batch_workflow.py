import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from tests.test_calculation import A,B,W,address
from tests.test_workflow import Fake
from payout_workflow import Workflow,PendingPayments
from payout_calculation import calculate
from payout_state import StateError
from batch_accounting import BatchStore
from payout_batch_state import linked_digest

class BatchFake(Fake):
    def __init__(self):
        super().__init__();self.batch_proofs={};self.batch_sent=[];self.batch_loss=False
    def batch_health(self,epoch=None):
        return dict(self.health(epoch=epoch),pooled_zkapps=[])
    def build_batch(self,expected):return json.dumps(expected)
    def sign_batch(self,unsigned,expected,password):
        self.calls.append('batch_sign')
        return {'signed':json.dumps({'expected':expected}),'tx_hash':'batch'+str(expected['nonce'])}
    def validate_batch(self,signed,expected):
        if json.loads(signed)['expected']!=expected:raise StateError('signature content')
        return {'tx_hash':'batch'+str(expected['nonce'])}
    def submit_batch(self,signed,expected):
        self.batch_sent.append(expected);self.nonce+=1
        self.liquid-=expected['fee_nano']+sum(r['amount_nano'] for r in expected['recipients'])
        if self.batch_loss:raise ConnectionError('lost response')
        return expected['tx_hash']
    def batch_proof(self,expected,required_depth=3):
        p=self.batch_proofs.get(expected['tx_hash'])
        return p if p and p['depth']>=required_depth else {'status':'pending' if p else 'unknown',**({'in_chain_status':p['status'],'depth':p['depth']} if p else {})}
    def include_batch(self,status='applied',depth=15):
        e=self.batch_sent[-1];self.batch_proofs[e['tx_hash']]={**e,'status':status,'on_best_chain':True,'depth':depth,'height':10,'block_hash':'block'}
        if status=='failed':self.liquid+=sum(r['amount_nano'] for r in e['recipients'])

class EpochBatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.fake=BatchFake()
        self.config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain','DEFAULT_TX_FEE':7,
                     'PAYOUT_TRANSPORT':'zkapp_batch','PAYOUT_BATCH_SIZE':2,'PAYOUT_BATCH_FEE_NANO':7,
                     'TX_CONFIRMATIONS_NUM':3,'MINIMUM_PAYOUT':'0','VALIDATOR_FEE':'0.1','VALIDATOR_FEE_SP':'0.1'}
        self.w=Workflow(self.config,self.root,adapter=self.fake);self.w.initialize()
    def plan(self,count=3):
        self.addresses=[address(i) for i in range(10,10+count)]
        self.c=calculate(self.config,3,8,[{'public_key':a,'balance':'1','timing':None} for a in self.addresses],
             [{'reward_nano':300,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}],base_coinbase_nano=300,carry={})
        self.digest=self.w.create_plan(8,self.c,{'ledger_hash':'ledger'})
        return self.digest
    def batchstore(self):return BatchStore(self.root/'.payout-state',create=False)
    def test_split_send_confirm_continue_and_settle_epoch(self):
        d=self.plan();p=self.w.store().get_plan(d)
        self.assertEqual(sum(x['fee_nano'] for x in p['payments']),14)
        self.w.send(d,password='test');self.assertEqual(len(self.fake.batch_sent),1)
        self.fake.include_batch()
        with self.assertRaises(PendingPayments):self.w.reconcile(d,commit=True)
        self.assertEqual(len(self.fake.batch_sent),2)
        self.fake.include_batch();result=self.w.reconcile(d,commit=True)
        self.assertEqual(result['balances_nano'],{})
        self.assertEqual(self.fake.liquid,730)
        self.assertEqual(self.w.store().list_intents(W),[])
        self.assertEqual(len(self.w.store().list_intents(plan_digest=d)),3)
        import owner_income
        income=owner_income.summary(self.w.store(),W,'chain')
        self.assertEqual(income['available_nano'],30)
        self.assertEqual(income['withdrawn_with_fees_nano'],0)
        self.w.send(d,password='unused');self.w.reconcile(d,commit=True)
        self.assertEqual(len(self.fake.batch_sent),2)
    def test_lost_response_restart_reconciles_then_sends_only_next_batch(self):
        d=self.plan();self.fake.batch_loss=True
        with self.assertRaises(Exception):self.w.send(d,password='test')
        self.fake.include_batch();self.fake.batch_loss=False
        restarted=Workflow(self.config,self.root,adapter=self.fake)
        restarted.send(d,password='test')
        self.assertEqual([x['nonce'] for x in self.fake.batch_sent],[4,5])
        self.fake.include_batch();restarted.reconcile(d,commit=True)
        self.assertEqual(restarted.carry(),{})
    def test_unknown_before_acceptance_is_not_retried_or_settled(self):
        d=self.plan()
        def lost(*args):raise ConnectionError('lost before acceptance')
        with patch.object(self.fake,'submit_batch',side_effect=lost) as submit:
            with self.assertRaises(Exception):self.w.send(d,password='test')
            self.w.send(d,password='test')
            with self.assertRaises(PendingPayments):self.w.reconcile(d,commit=True)
            self.assertEqual(submit.call_count,1)
    def test_failed_packet_charges_one_fee_and_carries_unpaid_recipient_rewards(self):
        d=self.plan();self.w.send(d,password='test');self.fake.include_batch(status='failed')
        result=self.w.reconcile(d,commit=True)
        self.assertEqual(sum(result['balances_nano'].values()),263)
        self.assertEqual(len(self.fake.batch_sent),1)
        self.assertEqual(self.fake.liquid,993)
        import owner_income
        income=owner_income.summary(self.w.store(),W,'chain')
        self.assertEqual(income['credits'][0]['delegator_fees_nano'],7)
        self.assertEqual(income['available_nano'],30)
    def test_dry_run_no_batch_journal_and_funding_protects_carry(self):
        d=self.plan();before=(self.root/'.payout-state/journal.sqlite3').read_bytes()
        self.w.send(d,dry_run=True)
        self.assertFalse((self.root/'.payout-state/batch-mainnet.sqlite3').exists())
        self.assertEqual((self.root/'.payout-state/journal.sqlite3').read_bytes(),before)
        self.fake.liquid=269
        with self.assertRaises(StateError):self.w.send(d,password='test')
        self.assertNotIn('batch_sign',self.fake.calls)
    def test_readonly_reconcile_does_not_settle_batch(self):
        d=self.plan();self.w.send(d,password='test');self.fake.include_batch()
        path=self.root/'.payout-state/batch-mainnet.sqlite3';before=path.read_bytes()
        self.w.reconcile(d,commit=False)
        self.assertEqual(path.read_bytes(),before)
    def test_policy_change_after_plan_does_not_switch_transport_or_fees(self):
        d=self.plan();self.config.update(PAYOUT_TRANSPORT='payment',PAYOUT_BATCH_SIZE=1,PAYOUT_BATCH_FEE_NANO=999)
        self.w.send(d,password='test')
        self.assertEqual(len(self.fake.batch_sent[0]['recipients']),2)
        self.assertEqual(self.fake.batch_sent[0]['fee_nano'],7)

    def test_lost_batch_database_after_send_cannot_recreate_or_resend(self):
        d=self.plan();self.w.send(d,password='test');self.fake.include_batch()
        (self.root/'.payout-state/batch-mainnet.sqlite3').unlink()
        with self.assertRaises(StateError):self.w.send(d,password='test')
        self.assertEqual(len(self.fake.batch_sent),1)

    def test_nonce_changes_while_password_entered_stops_before_submission(self):
        d=self.plan()
        def confirm(plan):self.fake.nonce+=1;return 'test'
        # The captured ordinary marker pins the starting nonce after the prompt,
        # while an untracked external spend is checked against the owner baseline.
        import owner_income
        owner_income.initialize_account(self.w.store(),W,'chain',self.fake.nonce)
        with self.assertRaises(StateError):self.w.send(d,confirm=confirm)
        self.assertEqual(self.fake.batch_sent,[])

    def test_next_batch_rechecks_reward_ancestry_after_password_wait(self):
        d=self.plan();self.w.send(d,password='test');self.fake.include_batch()
        self.fake.healthy=False
        with self.assertRaises(Exception):self.w.reconcile(d,commit=True)
        self.assertEqual(len(self.fake.batch_sent),1)

    def test_validator_funded_failure_and_cancellation_charge_owner_once(self):
        self.config['PAYOUT_NETWORK_FEE_SOURCE']='validator'
        d=self.plan();self.w.send(d,password='test');self.fake.include_batch(status='failed')
        result=self.w.reconcile(d,commit=True)
        self.assertEqual(sum(result['balances_nano'].values()),270)
        import owner_income
        income=owner_income.summary(self.w.store(),W,'chain')
        self.assertEqual(income['available_nano'],23)
        self.assertEqual(income['withdrawn_with_fees_nano'],0)

    def test_withdraw_after_batch_uses_owner_income_and_known_batch_nonce(self):
        d=self.plan(count=1);self.w.send(d,password='test');self.fake.include_batch()
        self.w.reconcile(d,commit=True)
        with patch('batch_archive.verify',side_effect=lambda config,expected,required_depth=3:self.fake.batch_proof(expected,required_depth)):
            result=self.w.withdraw(A,None,password='test',reuse_existing=True)
        p=self.w.store().get_plan(result['plan'])
        self.assertEqual(p['payments'][0]['amount_nano'],23)
        self.assertEqual(len(self.fake.batch_sent),1)

    def test_prepared_last_batch_still_needs_send_not_only_reconcile(self):
        from batch_mainnet import signer_packet
        d=self.plan(count=1)
        # Fail the final approval after signed intent is durably saved.
        original=self.fake.sign_batch
        def sign(*args):
            signed=original(*args);self.fake.liquid=0;return signed
        self.fake.sign_batch=sign
        with self.assertRaises(StateError):self.w.send(d,password='test')
        self.assertTrue(self.w.needs_send(d))
        self.fake.liquid=1000;self.fake.sign_batch=original
        self.w.send(d,password='unused');self.assertEqual(len(self.fake.batch_sent),1)
        self.assertEqual(self.fake.calls.count('batch_sign'),1)

    def test_normal_cli_complete_runs_all_packets_backups_and_idempotent_repeat(self):
        import io,contextlib,payout_cli
        d=self.plan()
        base_proof=self.fake.batch_proof
        def auto_proof(expected,required_depth=3):
            if self.fake.batch_sent and expected['tx_hash']==self.fake.batch_sent[-1]['tx_hash']:
                self.fake.include_batch()
            return base_proof(expected,required_depth)
        self.fake.batch_proof=auto_proof
        args=['send','--epoch','8','--complete','--yes','--project-root',str(self.root),'--wait-seconds','120']
        with patch('payout_cli.load_config',return_value=self.config),patch('tunnel.ensure'),\
             patch('payout_workflow.LiveAdapter',return_value=self.fake),\
             patch('batch_mainnet.read_password',return_value='test') as password,\
             patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(payout_cli.main(args),0)
            self.assertEqual(payout_cli.main(args),0)
        password.assert_called_once()
        self.assertEqual(len(self.fake.batch_sent),2)
        self.assertIsNotNone(self.w.store().settlement(d))
        self.assertGreaterEqual(len(list((self.root/'.payout-state/backups').glob('*/batch-mainnet.sqlite3'))),1)

    def test_existing_balance_report_reads_pending_batch_without_ordinary_nonce_probe(self):
        from payout_app import calculate_epoch
        d=self.plan();self.w.send(d,password='test')
        health=self.fake.health
        def reject_wallet(epoch=None,wallet=False):
            if wallet:raise AssertionError('ordinary nonce-gap probe')
            return health(epoch=epoch,wallet=wallet)
        self.fake.health=reject_wallet
        with patch('payout_app.live_block_report',return_value={'blocks':[]}):
            result=calculate_epoch(self.w,8)
        self.assertEqual(result['plan'],d)
        self.assertTrue(result['existing'])

    def test_no_recipient_epoch_settles_without_transaction_or_batch_file(self):
        self.config['MINIMUM_PAYOUT']='1'
        d=self.plan();self.assertFalse(self.w.needs_send(d))
        self.w.reconcile(d,commit=True)
        self.assertEqual(sum(self.w.carry().values()),270)
        self.assertFalse((self.root/'.payout-state/batch-mainnet.sqlite3').exists())
