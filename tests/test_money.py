"""Breaks caught: silent rounding, binary floats, invalid money and lost nanos."""
import unittest
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import money


class MoneyTests(unittest.TestCase):
    def test_exact_nanos_and_large_values(self):
        for value, expected in [('0.000000001', 1), ('12.345678901', 12345678901),
                                ('9007199254740993.000000001', 9007199254740993000000001)]:
            self.assertEqual(money.mina_to_nano(value), expected)
        self.assertEqual(money.nano_to_mina(12345678901), Decimal('12.345678901'))

    def test_fractional_nanos_require_explicit_rounding(self):
        with self.assertRaises(ValueError):
            money.mina_to_nano('0.0000000015')
        self.assertEqual(money.mina_to_nano('0.0000000015', rounding=ROUND_HALF_UP), 2)
        self.assertEqual(money.mina_to_nano('0.0000000015', rounding=ROUND_DOWN), 1)

    def test_invalid_or_negative_amounts_cannot_enter_payment(self):
        for value in ['NaN', 'Infinity', '-Infinity', '-0.1', '', True, 0.1, None]:
            with self.subTest(value=value), self.assertRaises((ValueError, TypeError)):
                money.mina_to_nano(value)
        self.assertEqual(money.mina_to_nano('-0.000000001', allow_negative=True), -1)
        with self.assertRaises((ValueError, TypeError)):
            money.nano_to_mina(0.5)

    def test_decimal_rates_remain_exact(self):
        self.assertEqual(money.parse_decimal('0.070000000000000000001'),
                         Decimal('0.070000000000000000001'))
