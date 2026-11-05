import tempfile
import unittest
from payout_state import StateStore, StateError

class SettlementTests(unittest.TestCase):
    def test_empty_payout_binds_identity_and_settles_once(self):
        with tempfile.TemporaryDirectory() as root:
            s=StateStore(root,create=True)
            p={'wallet':'wallet','network':'chain','era':'3','epoch':8,'payments':[], 'gross_nano':{'address':7}}
            d=s.create_plan(p)
            s.bind_plan(d)
            self.assertEqual(s.settle(d, {'address':7}), {'address':7})
            self.assertEqual(s.settle(d, {'address':7}), {'address':7})
            self.assertEqual(s.carry_balances('wallet','chain'), {'address':7})
            changed=s.create_plan(dict(p,gross_nano={'address':8}))
            with self.assertRaises(StateError): s.bind_plan(changed)
            with self.assertRaises(StateError): s.settle(d, {'address':8})
    def test_settlement_rejects_unresolved_payment(self):
        with tempfile.TemporaryDirectory() as root:
            s=StateStore(root,create=True)
            d=s.create_plan({'wallet':'w','network':'n','era':'3','epoch':1,'payments':[{'recipient':'r','amount_nano':9,'fee_nano':1}]})
            with self.assertRaises(StateError): s.settle(d, {'r':0})
    def test_empty_withdrawal_still_refused(self):
        with tempfile.TemporaryDirectory() as root:
            s=StateStore(root,create=True)
            with self.assertRaises(StateError): s.create_plan({'wallet':'w','network':'n','era':'3','epoch':1,'kind':'withdrawal','operation_id':'c80adc1c-b456-4f74-954e-039e842ddba7','payments':[]})
