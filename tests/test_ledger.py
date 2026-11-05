"""Breaks caught: empty-on-corruption, stale carry, float drift and torn saves."""
import json
from pathlib import Path
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

try:
    import ledger
except ModuleNotFoundError as exc:
    if exc.name != 'ledger':
        raise
    ledger = None


@unittest.skipIf(ledger is None, 'optional private ledger is not installed')
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.json'
        self.path.write_text('{"balances":{"A":0.000000001},"history":[{"epoch":0,"note":"old"}]}')

    def test_load_preserves_original_and_exact_legacy_money(self):
        original = self.path.read_bytes()
        self.assertEqual(ledger.load(self.path)['balances']['A'], Decimal('0.000000001'))
        self.assertEqual(self.path.read_bytes(), original)

    def test_missing_corrupt_and_missing_history_fail_closed(self):
        for content in [None, '{', '{"balances":{}}', '{"balances":{},"history":{}}',
                        '{"balances":{},"history":[],"balances":{"A":1}}',
                        '{"balances":{"A":"NaN"},"history":[]}',
                        '{"balances":{},"history":[{"epoch":1,"deltas":{"A":"x"}}]}']:
            with self.subTest(content=content):
                if content is None:
                    self.path.unlink(missing_ok=True)
                else:
                    self.path.write_text(content)
                with self.assertRaises((ValueError, OSError)):
                    ledger.load(self.path)

    def test_save_crash_before_replace_leaves_prior_bytes(self):
        original = self.path.read_bytes()
        with patch('os.replace', side_effect=OSError('simulated crash')):
            with self.assertRaises(OSError):
                ledger.save({'balances': {'A': Decimal('2.123456789')}, 'history': []}, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ['ledger.json'])

    def test_balance_adjustment_reloads_after_save(self):
        self.assertEqual(ledger.adjust(Decimal('1'), 'A', self.path),
                         (Decimal('1.000000001'), Decimal('0.000000001')))
        ledger.save({'balances': {'A': Decimal('-0.1')}, 'history': []}, self.path)
        self.assertEqual(ledger.adjust(Decimal('1'), 'A', self.path),
                         (Decimal('0.9'), Decimal('-0.1')))

    def test_delta_record_undo_preserve_sign_and_exactness(self):
        data = ledger.load(self.path)
        self.assertEqual(ledger.apply_delta('A', Decimal('-0.100000001'), data), Decimal('-0.1'))
        ledger.record(data, 1, 'reconcile', {'A': Decimal('-0.100000001')}, self.path)
        restored = ledger.load(self.path)
        self.assertEqual(ledger.undo(restored, 1), 1)
        self.assertEqual(restored['balances']['A'], Decimal('0.000000001'))
        self.assertEqual(ledger.undo(restored, 1), 0)
        self.assertEqual(len(ledger.unreversible(restored, 0)), 1)

    def test_explicit_initialize_never_replaces_history(self):
        with self.assertRaises(FileExistsError):
            ledger.initialize(self.path)
        fresh = self.path.parent / 'new.json'
        ledger.initialize(fresh)
        self.assertEqual(ledger.load(fresh), {'balances': {}, 'history': []})

    def test_legacy_float_callers_keep_presentation_and_nano_rounding(self):
        payout, carry = ledger.adjust(1.0, 'A', self.path)
        self.assertIsInstance(payout, float)
        self.assertIsInstance(carry, float)
        self.assertAlmostEqual(payout, 1.000000001)
        data = ledger.load(self.path)
        ledger.apply_delta('A', -0.10000000100000002, data)
        self.assertEqual(data['balances']['A'], Decimal('-0.100000000'))
