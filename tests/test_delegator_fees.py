"""Network costs belong to the frozen payer policy, including settlement."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import owner_income
from payout_calculation import calculate
from payout_workflow import Workflow
from tests.test_calculation import A,B,W
from tests.test_workflow import Fake


class DelegatorFeeTests(unittest.TestCase):
    def setUp(self):
        self.cfg={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain',
                  'DEFAULT_TX_FEE':10,'MINIMUM_PAYOUT':'0.000000050','VALIDATOR_FEE':'0.1',
                  'EXCLUDE_FROM_PAYOUTS':[],'TX_CONFIRMATIONS_NUM':3}

    def calculation(self,reward=100,carry=None):
        return calculate(self.cfg,3,8,[{'public_key':A,'balance':'1','timing':None}],
            [{'reward_nano':reward,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}],
            base_coinbase_nano=reward,carry=carry or {})

    def workflow(self):
        tmp=tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.fake=Fake(); self.w=Workflow(self.cfg,Path(tmp.name),adapter=self.fake); self.w.initialize()
        return self.w

    def test_default_new_calculation_charges_fee_after_validator_commission(self):
        result=self.calculation()
        self.assertEqual(result['policy']['network_fee_source'],'delegators')
        self.assertEqual(result['validator_nano'],10)
        self.assertEqual(result['gross_nano'],{A:90})
        self.assertEqual(result['net_nano'],{A:80})
        self.assertEqual(result['delegator_fees_nano'],{A:10})
        self.assertEqual(result['carry_after_nano'],{})
        self.assertEqual(sum(result['net_nano'].values())+sum(result['delegator_fees_nano'].values())+result['validator_nano'],100)

    def test_threshold_is_checked_after_fee_and_unsent_dust_pays_nothing(self):
        self.cfg['VALIDATOR_FEE']='0'
        for reward,expected,carry in [(20,{A:50},{}),(19,{}, {A:59})]:
            with self.subTest(reward=reward):
                result=self.calculation(reward,{A:40})
                self.assertEqual(result['net_nano'],expected)
                self.assertEqual(result['carry_after_nano'],carry)
                self.assertEqual(result['delegator_fees_nano'],{A:10} if expected else {})

    def test_zero_or_negative_net_is_carried_even_when_minimum_is_zero(self):
        self.cfg.update(VALIDATOR_FEE='0',MINIMUM_PAYOUT='0')
        for reward in (5,10):
            result=self.calculation(reward)
            self.assertEqual(result['net_nano'],{})
            self.assertEqual(result['delegator_fees_nano'],{})
            self.assertEqual(result['carry_after_nano'],{A:reward})

    def test_redirects_pay_one_fee_per_actual_recipient(self):
        self.cfg.update(VALIDATOR_FEE='0',PAYOUT_REDIRECTS={A:{W:'1'},B:{W:'1'}})
        result=calculate(self.cfg,3,8,[{'public_key':a,'balance':'1','timing':None} for a in (A,B)],
            [{'reward_nano':100,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}],base_coinbase_nano=100,carry={})
        self.assertEqual(result['net_nano'],{W:90})
        self.assertEqual(result['delegator_fees_nano'],{W:10})

    def test_confirmed_fee_is_not_carried_or_charged_to_owner_again(self):
        w=self.workflow(); result=self.calculation(); digest=w.create_plan(8,result,{})
        w.send(digest,password='test'); self.fake.include(3); w.reconcile(digest,commit=True)
        self.assertEqual(w.carry(),{})
        income=owner_income.summary(w.store(),W,'chain')
        self.assertEqual(income['available_nano'],10)
        self.assertEqual(income['credits'][0]['payout_fees_nano'],0)
        self.assertEqual(income['credits'][0]['delegator_fees_nano'],10)
        w.reconcile(digest,commit=True)
        self.assertEqual(owner_income.summary(w.store(),W,'chain')['available_nano'],10)

    def test_failed_payment_restores_full_entitlement_including_unspent_fee(self):
        result=self.calculation()
        plan={'gross_nano':result['gross_nano'],'carry_before_nano':{},'policy':result['policy'],
              'payments':[{'recipient':A,'amount_nano':80,'fee_nano':10}]}
        intents=[{'status':'failed','payment':plan['payments'][0]}]
        self.assertEqual(Workflow._payout_balances(plan,intents),{A:90})

    def test_frozen_legacy_plan_keeps_old_amount_and_owner_fee(self):
        self.cfg['PAYOUT_NETWORK_FEE_SOURCE']='validator'
        w=self.workflow(); result=self.calculation()
        result['policy'].pop('network_fee_source',None)
        digest=w.create_plan(8,result,{})
        self.cfg['PAYOUT_NETWORK_FEE_SOURCE']='delegators'
        w.send(digest,password='test'); self.fake.include(3); w.reconcile(digest,commit=True)
        self.assertEqual(w.store().get_plan(digest)['payments'][0]['amount_nano'],90)
        self.assertEqual(w.carry(),{})
        income=owner_income.summary(w.store(),W,'chain')
        self.assertEqual(income['available_nano'],0)
        self.assertEqual(income['credits'][0]['payout_fees_nano'],10)

    def test_unknown_fee_source_is_rejected(self):
        self.cfg['PAYOUT_NETWORK_FEE_SOURCE']='invalid'
        with self.assertRaises(ValueError): self.calculation()

    def test_balance_display_explains_fee_in_conservation_equation(self):
        from payout_app import report
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            report({'epoch':8,'era':'3','preview':True,'calculation':self.calculation()})
        self.assertIn('Сеть',output.getvalue())
        self.assertIn('К выплате + Сеть + Остаток после',output.getvalue())
