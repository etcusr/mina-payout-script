"""Mina byte-string token symbols must not break exact staking allocations."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import GraphQL
from tests.test_calculation import A,W


class LedgerEncodingTests(unittest.TestCase):
    def load(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            cache=Path(directory); path=cache/'epoch-8-test.json'; path.write_bytes(payload)
            with patch.object(GraphQL,'_LEDGERS_CACHE',cache),patch.object(GraphQL,'_resolve_epoch_for_hash',return_value=8):
                result=GraphQL.getStakingLedger({'delegate':W,'ledgerHash':'test'})
            self.assertEqual(path.read_bytes(),payload)
            return result['data']['stakes']

    def fixture(self):
        return json.dumps([{'pk':A,'delegate':W,'balance':'12.000000001','token_symbol':'BINARY','timing':None}]).encode()

    def test_binary_token_symbol_preserves_exact_balance_and_original_cache(self):
        stakes=self.load(self.fixture().replace(b'BINARY',b'\xff'))
        self.assertEqual(stakes[0]['balance'],Decimal('12.000000001'))
        self.assertEqual(stakes[0]['public_key'],A)

    def test_invalid_bytes_outside_token_symbol_are_rejected(self):
        for field in ('delegate','pk','balance'):
            data=json.loads(self.fixture()); data[0][field]='BINARY'
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.load(json.dumps(data).encode().replace(b'BINARY',b'\xff'))

    def test_broken_json_is_not_silently_repaired(self):
        with self.assertRaises(ValueError): self.load(self.fixture()[:-1])
