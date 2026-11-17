"""Before signing, only fresh health reads may retry archive ingestion lag."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import owner_income
import preflight
from payout_state import StateError
from payout_workflow import Workflow
from tests.test_calculation import A,W
from tests.test_workflow import Fake


class PresendArchiveRetryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.fake=Fake(); self.fake.liquid=51
        self.w=Workflow({'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain',
                         'DEFAULT_TX_FEE':1,'TX_CONFIRMATIONS_NUM':3},Path(tmp.name),adapter=self.fake)
        self.w.initialize(); store=self.w.store()
        owner_income.initialize_account(store,W,'chain',self.fake.nonce)
        owner_income.record_credit(store,{'wallet':W,'network':'chain','era':'3','epoch':6,
            'validator_nano':118,'retained_own_nano':0,'payout_fees_nano':0,'own_paid_nano':0})
        self.original_health=self.fake.health; self.after_prompt=[]; self.seen=[]

    def confirm_with_errors(self,errors,*,new_liquid=None):
        def confirm(plan):
            self.seen.append(plan)
            def health(*args,**kwargs):
                self.after_prompt.append(True)
                if errors: raise errors.pop(0)
                if new_liquid is not None: self.fake.liquid=new_liquid
                return self.original_health(*args,**kwargs)
            self.fake.health=health
            return 'test'
        return confirm

    def test_post_password_lag_recovers_before_unlock_without_recreating_plan(self):
        confirm=self.confirm_with_errors([preflight.PreflightArchiveLagError('lag')],new_liquid=1000)
        output=io.StringIO()
        with patch('time.sleep') as sleep,patch.object(self.w,'_verify_previous',wraps=self.w._verify_previous) as previous,contextlib.redirect_stdout(output):
            result=self.w.withdraw(A,None,confirm=confirm,reuse_existing=True)
        self.assertEqual(len(self.after_prompt),2); sleep.assert_called_once_with(15)
        self.assertEqual(previous.call_count,3)
        self.assertEqual(self.fake.calls.count('unlock'),1)
        self.assertEqual(sum(isinstance(c,tuple) for c in self.fake.calls),1)
        self.assertEqual(self.w.store().get_plan(result['plan']),self.seen[0])
        self.assertEqual(self.seen[0]['payments'][0]['amount_nano'],50)
        self.assertEqual(output.getvalue().count('Архив ещё'),1)
        self.assertIn('догнал',output.getvalue())

    def test_persistent_lag_stops_after_three_reads_without_nonce_or_send(self):
        confirm=self.confirm_with_errors([preflight.PreflightArchiveLagError('lag') for _ in range(3)])
        with patch('time.sleep') as sleep,contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(preflight.PreflightArchiveLagError):
                self.w.withdraw(A,None,confirm=confirm,reuse_existing=True)
        self.assertEqual(len(self.after_prompt),3); self.assertEqual(sleep.call_count,2)
        self.assertNotIn('unlock',self.fake.calls); self.assertEqual(self.w.store().list_intents(),[])
        self.assertEqual(self.w.store().list_plans()[0][1],self.seen[0])

    def test_mismatch_or_orphan_or_connection_error_does_not_retry_before_send(self):
        for error in (preflight.PreflightError('mismatch'),preflight.PreflightError('orphaned'),preflight.PreflightConnectionError('offline')):
            with self.subTest(error=str(error)),patch('time.sleep') as sleep:
                self.fake.health=self.original_health; self.after_prompt=[]
                with self.assertRaises(type(error)):
                    self.w.withdraw(A,None,confirm=self.confirm_with_errors([error]),reuse_existing=True)
                self.assertEqual(len(self.after_prompt),1); sleep.assert_not_called()
                self.assertNotIn('unlock',self.fake.calls)

    def test_funds_are_rechecked_after_recovery_and_can_block_send(self):
        confirm=self.confirm_with_errors([preflight.PreflightArchiveLagError('lag')],new_liquid=50)
        with patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(StateError,'Недостаточно'):
                self.w.withdraw(A,None,confirm=confirm,reuse_existing=True)
        self.assertEqual(len(self.after_prompt),2)
        self.assertNotIn('unlock',self.fake.calls); self.assertEqual(self.w.store().list_intents(),[])
