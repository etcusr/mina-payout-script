"""3-block settlement and 15-block next-epoch gate, fake transport only."""
import tempfile
import importlib.util
import unittest
from pathlib import Path
from tests.test_workflow import Fake
from tests.test_calculation import A,W
from payout_workflow import Workflow,PendingPayments
from payout_state import StateError


class ConfirmationPolicyTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec('ledger'),'optional private ledger is absent')
    def test_imported_history_without_receipts_cannot_bypass_previous_check(self):
        import ledger
        other=self.root/'legacy'; other.mkdir()
        ledger.save({'balances':{},'history':[{'epoch':6,'note':'old paid epoch'}]},other/'payout_ledger.json')
        workflow=Workflow(dict(self.config,LEGACY_PROTOCOL_VERSION_ID=3),other,adapter=self.fake)
        workflow.initialize(import_legacy=True)
        with self.assertRaisesRegex(StateError,'квитанц'):
            workflow.create_plan(8,{'gross_nano':{},'net_nano':{},'carry_before_nano':{},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
        self.assertNotIn('unlock',self.fake.calls)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.fake=Fake()
        self.config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain',
                     'DEFAULT_TX_FEE':1,'TX_CONFIRMATIONS_NUM':3,'PREVIOUS_PAYOUT_CONFIRMATIONS_NUM':15}
        self.w=Workflow(self.config,self.root,adapter=self.fake); self.w.initialize()

    def plan(self,epoch=7):
        return self.w.create_plan(epoch,{'gross_nano':{A:100},'net_nano':{A:100},
            'carry_before_nano':{},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})

    def paid(self):
        digest=self.plan(); self.w.send(digest,password='test'); self.fake.include(3)
        self.w.reconcile(digest,commit=True)
        self.fake.calls=[]
        return digest

    def test_two_blocks_pending_three_blocks_settle(self):
        digest=self.plan(); self.w.send(digest,password='test'); self.fake.include(2)
        with self.assertRaises(PendingPayments): self.w.reconcile(digest,commit=True)
        self.fake.chain[0]['depth']=3
        self.assertEqual(self.w.reconcile(digest,commit=True)['balances_nano'],{})
        self.assertEqual(self.w.store().list_intents()[0]['evidence']['required_depth'],3)

    def test_existing_twenty_block_plan_uses_explicit_new_policy_without_rehash(self):
        self.w.config['TX_CONFIRMATIONS_NUM']=20
        digest=self.plan(); original=self.w.store().get_plan(digest)
        self.w.send(digest,password='test'); self.fake.include(3)
        self.w.config['TX_CONFIRMATIONS_NUM']=3
        self.w.reconcile(digest,commit=True)
        self.assertEqual(self.w.store().get_plan(digest),original)
        self.assertEqual(original['min_confirmations'],20)
        self.assertEqual(self.w.store().list_intents()[0]['evidence']['required_depth'],3)

    def test_fourteen_blocks_block_next_epoch_fifteen_allow(self):
        self.paid(); self.fake.chain[0]['depth']=14
        with self.assertRaises(StateError): self.plan(epoch=8)
        self.assertNotIn('unlock',self.fake.calls)
        self.fake.chain[0]['depth']=15
        next_digest=self.plan(epoch=8)
        self.w.send(next_digest,password='test')
        self.assertEqual(sum(isinstance(c,tuple) for c in self.fake.calls),1)

    def test_era_reset_does_not_bypass_previous_fifteen_block_gate(self):
        self.paid()
        old_health=self.fake.health
        self.fake.health=lambda *a,**kw: dict(old_health(*a,**kw),era=4,current_epoch=1,epoch_closed=True)
        with self.assertRaises(StateError): self.plan(epoch=0)
        self.fake.chain[0]['depth']=15
        self.assertIsInstance(self.plan(epoch=0),str)

    def test_previous_payment_disappears_after_plan_before_send(self):
        self.paid(); self.fake.chain[0]['depth']=15
        next_digest=self.plan(epoch=8)
        self.fake.chain=[]
        with self.assertRaises(StateError): self.w.send(next_digest,password='test')
        self.assertNotIn('unlock',self.fake.calls)

    def test_previous_payment_changes_while_user_enters_password(self):
        self.paid(); self.fake.chain[0]['depth']=15
        next_digest=self.plan(epoch=8)
        def confirm(plan):
            self.fake.chain[0]['tx_hash']='different-hash'
            return 'test'
        with self.assertRaises(StateError): self.w.send(next_digest,confirm=confirm)
        self.assertNotIn('unlock',self.fake.calls)

    def test_withdraw_rechecks_previous_at_three_without_waiting_fifteen(self):
        self.paid()
        self.w.withdraw(A,10,password='test')
        self.assertIn('unlock',self.fake.calls)

    def test_withdraw_is_blocked_when_previous_payment_disappears(self):
        self.paid(); self.fake.chain=[]
        with self.assertRaises(StateError): self.w.withdraw(A,10,password='test')
        self.assertNotIn('unlock',self.fake.calls)
        self.assertEqual(len(self.w.store().list_plans()),1)

    def test_failed_or_off_chain_proof_never_completes(self):
        digest=self.plan(); self.w.send(digest,password='test'); self.fake.include(3)
        for key,value in (('status','failed'),('on_best_chain',False)):
            original=self.fake.chain[0][key]; self.fake.chain[0][key]=value
            with self.assertRaises(PendingPayments): self.w.reconcile(digest,commit=True)
            self.fake.chain[0][key]=original
        self.assertIsNone(self.w.store().settlement(digest))
