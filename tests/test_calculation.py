import hashlib
import unittest
from decimal import Decimal
try:
    import payout_calculation as calc
except ModuleNotFoundError:
    calc=None

ALPHABET='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
def address(seed):
    body=bytes([203,1,1])+bytes([seed])*33
    raw=body+hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]
    n=int.from_bytes(raw,'big'); out=''
    while n: n,r=divmod(n,58); out=ALPHABET[r]+out
    return out
A,B,W=address(1),address(2),address(3)

class CalculationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(calc, 'exact payout calculation module required')
        self.cfg={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'VALIDATOR_FEE':'0.1','VALIDATOR_FEE_SP':'0.2','VALIDATOR_FEE_FOUNDATION':'0.3','VALIDATOR_FEE_O1LABS':'0.4','DEFAULT_TX_FEE':1,'MINIMUM_PAYOUT':'0.000000010','EXCLUDE_FROM_PAYOUTS':[]}
    def test_floor_allocations_conserve_reward_and_carry_dust(self):
        r=calc.calculate(self.cfg,3,8,[{'public_key':A,'balance':'1','timing':None},{'public_key':B,'balance':'2','timing':None}], [{'reward_nano':101,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}], base_coinbase_nano=101,carry={A:-25,B:3})
        self.assertEqual(r['gross_nano'],{A:30,B:60})
        self.assertEqual(r['net_nano'],{B:63})
        self.assertEqual(r['validator_nano'],11)
        self.assertEqual(r['carry_after_nano'],{A:5})
    def test_redirect_last_remainder_and_own_exclusion(self):
        self.cfg.update(VALIDATOR_FEE='0',PAYOUT_REDIRECTS={A:{B:'1',W:'2'}},EXCLUDE_FROM_PAYOUTS=[W])
        r=calc.calculate(self.cfg,3,8,[{'public_key':A,'balance':'1','timing':None}],[{'reward_nano':101,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}],base_coinbase_nano=101,carry={})
        self.assertEqual(r['gross_nano'],{B:33})
        self.assertEqual(r['excluded_nano'],68)
        self.assertEqual(r['validator_nano'],0)
    def test_supercharged_special_winner_keeps_extra(self):
        r=calc.calculate(self.cfg,3,8,[{'public_key':A,'balance':'1','timing':None}],[{'reward_nano':200,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A}],base_coinbase_nano=100,carry={},foundation=[A])
        self.assertEqual(r['gross_nano'],{A:70})
        self.assertEqual(r['validator_nano'],130)
    def test_empty_rewards_keep_old_positive_obligations(self):
        r=calc.calculate(self.cfg,3,8,[],[],base_coinbase_nano=100,carry={A:7})
        self.assertEqual(r['net_nano'],{})
        self.assertEqual(r['carry_after_nano'],{A:7})
    def test_policy_schedule_freezes_effective_commission(self):
        self.cfg['COMMISSION_SCHEDULE']=[{'era':3,'from_epoch':0,'VALIDATOR_FEE':'0.05'},{'era':3,'from_epoch':9,'VALIDATOR_FEE':'0.1'}]
        self.assertEqual(calc.policy(self.cfg,3,8)['VALIDATOR_FEE'],'0.05')
        self.assertEqual(calc.policy(self.cfg,3,9)['VALIDATOR_FEE'],'0.1')
        with self.assertRaises(ValueError): calc.policy(self.cfg,2,8)
    def test_invalid_money_address_and_redirect_fail_closed(self):
        for value in ['NaN','-1','1.1']:
            with self.subTest(value=value):
                self.cfg['VALIDATOR_FEE']=value
                with self.assertRaises(ValueError): calc.policy(self.cfg,3,8)
        calc.validate_address(A)
        for bad in [A[:-1]+'1','B62q...',None]:
            with self.assertRaises(ValueError): calc.validate_address(bad)
        import redirects
        with self.assertRaises(ValueError): redirects.parse({A:{B:'NaN'}})
        self.assertEqual(redirects.split(11, redirects.parse({A:{B:1,W:2}})[A]),[(B,3),(W,8)])
