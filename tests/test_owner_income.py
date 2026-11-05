import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from tests.test_workflow import Fake
from tests.test_calculation import A,W
from payout_workflow import Workflow
from payout_state import StateError


class OwnerIncomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.fake=Fake(); self.fake.liquid=1000000
        self.cfg={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'WITHDRAW_TO_ADDRESS':A,
                  'EXPECTED_CHAIN_ID':'chain','DEFAULT_TX_FEE':1,'TX_CONFIRMATIONS_NUM':3}
        self.w=Workflow(self.cfg,self.root,adapter=self.fake); self.w.initialize()

    def credit(self,epoch=6,commission=100,own=20,fees=2):
        import owner_income
        store=self.w.store()
        owner_income.initialize_account(store,W,'chain',self.fake.nonce)
        owner_income.record_credit(store,{'wallet':W,'network':'chain','era':'3','epoch':epoch,
            'validator_nano':commission,'retained_own_nano':own,'payout_fees_nano':fees,
            'own_paid_nano':50,'own_retained_by_address':{W:own},'source':{'kind':'test'}})

    def test_auto_amount_includes_own_income_minus_payout_and_withdrawal_fees(self):
        self.credit()
        result=self.w.withdraw(A,None,password='test',reuse_existing=True)
        payment=self.w.store().get_plan(result['plan'])['payments'][0]
        self.assertEqual(payment['amount_nano'],117)
        self.assertEqual(payment['fee_nano'],1)

    def test_successful_auto_withdraw_is_never_sent_twice(self):
        self.credit(); first=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.fake.include(3); self.w.reconcile(first['plan'],commit=True)
        again=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertTrue(again['nothing_to_withdraw'])
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    def test_pending_auto_withdraw_reuses_plan_even_if_new_income_arrives(self):
        self.credit(); first=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.credit(epoch=7)
        again=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertEqual(first['plan'],again['plan'])
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    def test_lost_response_reserves_income_and_never_creates_second_transfer(self):
        self.credit(); self.fake.fail='submit'
        with self.assertRaises(StateError): self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.fake.fail=None
        self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    def test_same_amount_from_new_epoch_is_new_income_not_duplicate_of_old_plan(self):
        self.credit(); first=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.fake.include(3); self.w.reconcile(first['plan'],commit=True)
        self.credit(epoch=7)
        second=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertNotEqual(first['plan'],second['plan'])
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),2)

    def test_manual_withdraw_is_deducted_from_auto_balance(self):
        self.credit(); result=self.w.withdraw(A,50,password='test',reuse_existing=True)
        self.fake.include(3); self.w.reconcile(result['plan'],commit=True)
        result=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertEqual(self.w.store().get_plan(result['plan'])['payments'][0]['amount_nano'],66)

    def test_credit_cannot_be_added_twice_or_replaced(self):
        import owner_income
        self.credit(); self.credit()
        self.assertEqual(owner_income.summary(self.w.store(),W,'chain')['available_nano'],118)
        with self.assertRaises(StateError): self.credit(commission=101)

    def test_unknown_external_nonce_after_baseline_blocks_auto_withdraw(self):
        self.credit(); self.fake.nonce+=1
        with self.assertRaises(StateError): self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertNotIn('unlock',self.fake.calls)

    def test_external_spend_during_password_prompt_blocks_unlock(self):
        self.credit()
        def confirm(plan):
            self.fake.nonce+=1
            return 'test'
        with self.assertRaises(StateError): self.w.withdraw(A,None,confirm=confirm,reuse_existing=True)
        self.assertNotIn('unlock',self.fake.calls)

    def test_dry_run_does_not_reserve_or_send(self):
        self.credit(); before=(self.root/'.payout-state/journal.sqlite3').read_bytes()
        result=self.w.withdraw(A,None,dry_run=True,reuse_existing=True)
        self.assertEqual(result['amount_nano'],117)
        self.assertEqual(before,(self.root/'.payout-state/journal.sqlite3').read_bytes())
        self.assertNotIn('unlock',self.fake.calls)

    def test_restoring_pre_withdraw_snapshot_cannot_repeat_income_withdrawal(self):
        self.credit(); path=self.root/'.payout-state/journal.sqlite3'; old=path.read_bytes()
        first=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.fake.include(3); self.w.reconcile(first['plan'],commit=True)
        path.write_bytes(old)
        with self.assertRaises(StateError): self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertEqual(sum(isinstance(x,tuple) for x in self.fake.calls),1)

    def test_schema_two_upgrade_preserves_existing_plan_and_intents(self):
        from payout_state import StateStore
        self.credit()
        first=self.w.withdraw(A,10,password='test',reuse_existing=True)
        store=self.w.store(); plan=store.get_plan(first['plan']); intents=store.list_intents()
        with store._connection() as db:
            for table in ('owner_withdrawals','owner_earnings','owner_accounts'): db.execute('DROP TABLE '+table)
            db.execute('UPDATE metadata SET version=2')
        upgraded=StateStore(self.root/'.payout-state')
        self.assertEqual(upgraded.get_plan(first['plan']),plan)
        self.assertEqual(upgraded.list_intents(),intents)

    def test_missing_credit_after_settlement_is_recovered_before_auto_amount(self):
        self.credit()
        digest=self.w.create_plan(7,{'gross_nano':{A:60},'net_nano':{A:60},'carry_before_nano':{},'carry_after_nano':{},
            'validator_nano':20,'excluded_nano':20,'total_reward_nano':100,'excluded_by_address_nano':{W:20},
            'policy':{'fee_nano':1,'exclusions':[W]}}, {})
        self.w.send(digest,password='test'); self.fake.include(3)
        with patch('owner_income.record_payout',side_effect=OSError('simulated credit failure')):
            with self.assertRaises(OSError): self.w.reconcile(digest,commit=True)
        self.assertIsNotNone(self.w.store().settlement(digest))
        result=self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertEqual(self.w.store().get_plan(result['plan'])['payments'][0]['amount_nano'],156)

    def test_auto_does_not_implicitly_resume_oversized_manual_withdrawal(self):
        self.credit()
        with self.assertRaises(StateError): self.w.withdraw(A,500,reuse_existing=True)
        with self.assertRaisesRegex(StateError,'вручную'):
            self.w.withdraw(A,None,password='test',reuse_existing=True)
        self.assertFalse(any(isinstance(x,tuple) for x in self.fake.calls))

    def test_explicit_resume_keeps_auto_guards_after_reservation_crash(self):
        self.credit()
        with patch('owner_income.reserve_withdrawal',side_effect=OSError('crash before reservation')):
            with self.assertRaises(OSError): self.w.withdraw(A,None,password='test',reuse_existing=True)
        digest,plan=self.w.store().list_plans(W,'chain')[0]
        self.fake.nonce+=1
        with self.assertRaises(StateError):
            self.w.withdraw(A,117,password='test',operation_id=plan['operation_id'],reuse_existing=True)
        self.assertNotIn('unlock',self.fake.calls)
