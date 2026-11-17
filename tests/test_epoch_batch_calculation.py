"""Epoch fee allocation stays exact across deterministic batch regrouping."""
import unittest

import payout_calculation as calc
from tests.test_calculation import address, W


class EpochBatchCalculationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {'SEND_FROM_ADDRESS': W, 'VALIDATOR_ADDRESS': W,
                    'EXCLUDE_FROM_PAYOUTS': [], 'DEFAULT_TX_FEE': 10,
                    'PAYOUT_TRANSPORT': 'zkapp_batch', 'PAYOUT_BATCH_SIZE': 30,
                    'PAYOUT_BATCH_FEE_NANO': 61, 'MINIMUM_PAYOUT': '0'}
        self.addresses = sorted(address(i) for i in range(10, 75))

    def calculate(self, carry):
        return calc.calculate(self.cfg, 3, 10, [], [], base_coinbase_nano=100, carry=carry)

    def assert_conservation(self, result):
        for recipient in set(result['carry_before_nano']) | set(result['gross_nano']):
            self.assertEqual(result['gross_nano'].get(recipient, 0) + result['carry_before_nano'].get(recipient, 0),
                             result['net_nano'].get(recipient, 0) + result['carry_after_nano'].get(recipient, 0)
                             + result['delegator_fees_nano'].get(recipient, 0))

    def test_sixty_five_recipients_pay_three_exact_fees_in_sorted_groups(self):
        result = self.calculate({a: 1000 for a in reversed(self.addresses)})
        shares = [result['payment_fees_nano'][a] for a in self.addresses]
        self.assertEqual(shares, [3] + [2] * 29 + [3] + [2] * 29 + [13] + [12] * 4)
        self.assertEqual(sum(shares), 183)
        self.assertEqual(sum(result['net_nano'].values()), 64817)
        self.assertEqual(result['delegator_fees_nano'], result['payment_fees_nano'])
        self.assertEqual(result['carry_after_nano'], {})
        self.assert_conservation(result)

    def test_remainder_goes_to_earliest_addresses_and_zero_shares_are_preserved(self):
        self.cfg.update(PAYOUT_BATCH_FEE_NANO=2, PAYOUT_BATCH_SIZE=3)
        a, b, c = self.addresses[:3]
        result = self.calculate({c: 10, a: 10, b: 10})
        self.assertEqual(result['payment_fees_nano'], {a: 1, b: 1, c: 0})
        self.assertEqual(result['net_nano'], {a: 9, b: 9, c: 10})
        self.assert_conservation(result)

    def test_dust_removal_repeats_until_all_selected_nets_meet_minimum(self):
        self.cfg.update(PAYOUT_BATCH_FEE_NANO=10, PAYOUT_BATCH_SIZE=3,
                        MINIMUM_PAYOUT='0.000000010')
        a, b, c, d, e = self.addresses[:5]
        # Initial shares 4/3/3 remove a; next shares 5/5 remove b.
        result = self.calculate({a: 13, b: 14, c: 100, d: 9, e: -3})
        self.assertEqual(result['net_nano'], {c: 90})
        self.assertEqual(result['payment_fees_nano'], {c: 10})
        self.assertEqual(result['delegator_fees_nano'], {c: 10})
        self.assertEqual(result['carry_after_nano'], {a: 13, b: 14, d: 9, e: -3})
        self.assert_conservation(result)

    def test_removed_candidates_regroup_across_original_batch_boundaries(self):
        self.cfg.update(PAYOUT_BATCH_FEE_NANO=10, PAYOUT_BATCH_SIZE=2)
        a, b, c = self.addresses[:3]
        result = self.calculate({a: 5, b: 100, c: 100})
        self.assertEqual(result['payment_fees_nano'], {b: 5, c: 5})
        self.assertEqual(result['net_nano'], {b: 95, c: 95})
        self.assertEqual(result['carry_after_nano'], {a: 5})
        self.assert_conservation(result)

    def test_all_dust_or_no_candidates_produces_no_fees(self):
        self.cfg.update(PAYOUT_BATCH_FEE_NANO=10, PAYOUT_BATCH_SIZE=3)
        a, b = self.addresses[:2]
        for carry in ({a: 5, b: 5}, {a: 0, b: -1}, {}):
            with self.subTest(carry=carry):
                result = self.calculate(carry)
                self.assertEqual(result['net_nano'], {})
                self.assertEqual(result['payment_fees_nano'], {})
                self.assertEqual(result['delegator_fees_nano'], {})
                self.assertEqual(result['carry_after_nano'], {k: v for k, v in carry.items() if v})
                self.assert_conservation(result)

    def test_validator_funded_batch_preserves_owed_and_reports_actual_fee_shares(self):
        self.cfg.update(PAYOUT_NETWORK_FEE_SOURCE='validator', PAYOUT_BATCH_FEE_NANO=10,
                        PAYOUT_BATCH_SIZE=2, MINIMUM_PAYOUT='0.000000010')
        a, b, c, d = self.addresses[:4]
        result = self.calculate({a: 10, b: 11, c: 12, d: 9})
        self.assertEqual(result['net_nano'], {a: 10, b: 11, c: 12})
        self.assertEqual(result['payment_fees_nano'], {a: 5, b: 5, c: 10})
        self.assertEqual(result['delegator_fees_nano'], {})
        self.assertEqual(result['carry_after_nano'], {d: 9})
        self.assert_conservation(result)

    def test_batch_policy_freezes_default_size_and_fallback_fee(self):
        self.cfg.pop('PAYOUT_BATCH_SIZE')
        self.cfg.pop('PAYOUT_BATCH_FEE_NANO')
        p = calc.policy(self.cfg, 3, 10)
        self.assertEqual((p['transport'], p['batch_size'], p['fee_nano']), ('zkapp_batch', 30, 10))
        self.cfg.update(PAYOUT_BATCH_SIZE=1, PAYOUT_BATCH_FEE_NANO=7)
        p = calc.policy(self.cfg, 3, 10)
        self.assertEqual((p['batch_size'], p['fee_nano']), (1, 7))

    def test_invalid_transport_size_and_batch_fee_fail_closed(self):
        for key, values in [('PAYOUT_TRANSPORT', ('unknown', '', None, 1)),
                            ('PAYOUT_BATCH_SIZE', (0, -1, 31, True, '30', 1.5, None)),
                            ('PAYOUT_BATCH_FEE_NANO', (0, -1, True, '10', 1.5, None, 2**64))]:
            for value in values:
                with self.subTest(key=key, value=value):
                    cfg = dict(self.cfg, **{key: value})
                    with self.assertRaises(ValueError):
                        calc.policy(cfg, 3, 10)
        self.cfg.pop('PAYOUT_BATCH_FEE_NANO')
        self.cfg['DEFAULT_TX_FEE'] = 0
        with self.assertRaises(ValueError):
            calc.policy(self.cfg, 3, 10)

    def test_ordinary_payment_keeps_full_fee_and_legacy_policy_shape(self):
        self.cfg.pop('PAYOUT_TRANSPORT')
        a, b, c = self.addresses[:3]
        result = self.calculate({a: 20, b: 10, c: -1})
        self.assertNotIn('transport', result['policy'])
        self.assertNotIn('batch_size', result['policy'])
        self.assertEqual(result['policy']['fee_nano'], 10)
        self.assertEqual(result['net_nano'], {a: 10})
        self.assertEqual(result['payment_fees_nano'], {a: 10})
        self.assertEqual(result['delegator_fees_nano'], {a: 10})
        self.assertEqual(result['carry_after_nano'], {b: 10, c: -1})
        self.assert_conservation(result)
        self.cfg.update(PAYOUT_TRANSPORT='payment', PAYOUT_NETWORK_FEE_SOURCE='validator')
        result = self.calculate({a: 20, b: 10})
        self.assertEqual(result['policy']['transport'], 'payment')
        self.assertEqual(result['payment_fees_nano'], {a: 10, b: 10})
        self.assertEqual(result['delegator_fees_nano'], {})
        self.assertEqual(result['net_nano'], {a: 20, b: 10})
        self.assert_conservation(result)
