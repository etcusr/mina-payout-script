"""Breaks caught: duplicate submission/nonce, lost uncertainty, tampered plans,
concurrent writers, damaged restores and accidental historical sends.
"""
import json
import hashlib
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import payout_state as state


def sample_plan(epoch=7, amount=123456789):
    return {'wallet': 'TEST-WALLET', 'network': 'testnet', 'era': 'mesa', 'epoch': epoch,
            'payments': [{'recipient': 'TEST-RECIPIENT', 'amount_nano': amount,
                          'fee_nano': 10000000, 'memo': 'Epoch 7'}],
            'policy_digest': 'fixture-policy', 'input_digest': 'fixture-input'}


class PayoutStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.store = state.StateStore(self.root, create=True)

    def test_restart_preserves_uncertain_intent_and_prevents_retry(self):
        digest = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_unknown(intent['id'], 'response lost')
        restarted = state.StateStore(self.root)
        self.assertEqual(restarted.get_intent(intent['id'])['status'], 'unknown')
        self.assertEqual(restarted.pending_commitments('TEST-WALLET'), 133456789)
        with self.assertRaises(state.StateError):
            restarted.reserve_intent(digest, 0, 42)
        with self.assertRaises(state.StateError):
            restarted.reserve_intent(digest, 0, 43)

    def test_nonce_cannot_be_reserved_for_another_plan(self):
        first = self.store.create_plan(sample_plan())
        second = self.store.create_plan(sample_plan(epoch=8))
        self.store.reserve_intent(first, 0, 42)
        with self.assertRaises(state.StateError):
            self.store.reserve_intent(second, 0, 42)
        self.assertEqual(len(self.store.list_intents()), 1)

    def test_receipt_and_confirmation_survive_restart(self):
        digest = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_unknown(intent['id'], 'starting submission')
        self.store.mark_submitted(intent['id'], 'TX-HASH')
        self.store.mark_confirmed(intent['id'], {'tx_hash': 'TX-HASH', 'block_hash': 'BLOCK', 'depth': 20,
            'on_best_chain': True, 'status': 'applied', 'wallet': 'TEST-WALLET', 'recipient': 'TEST-RECIPIENT',
            'amount_nano': 123456789, 'fee_nano': 10000000, 'memo': 'Epoch 7',
            'nonce': 42, 'network': 'testnet', 'era': 'mesa'})
        reopened = state.StateStore(self.root)
        self.assertEqual(reopened.get_intent(intent['id'])['status'], 'confirmed')
        self.assertEqual(reopened.pending_commitments('TEST-WALLET'), 0)
        with self.assertRaises(state.StateError):
            reopened.mark_unknown(intent['id'], 'retry')

    def test_failed_resolution_requires_evidence_and_cannot_resend(self):
        digest = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_unknown(intent['id'], 'timeout')
        for evidence in [{}, {'not_found': True}, {'kind': 'nonce_consumed', 'depth': 20}]:
            with self.assertRaises(state.StateError):
                self.store.resolve_failed(intent['id'], evidence)
        self.store.resolve_failed(intent['id'], {'kind': 'nonce_consumed', 'tx_hash': 'OTHER-TX',
            'wallet': 'TEST-WALLET', 'nonce': 42, 'block_hash': 'BLOCK', 'depth': 20,
            'on_best_chain': True, 'status': 'applied', 'network': 'testnet', 'era': 'mesa',
            'recipient': 'DIFFERENT', 'amount_nano': 123456789, 'fee_nano': 10000000, 'memo': 'Epoch 7'})
        self.assertEqual(self.store.pending_commitments('TEST-WALLET'), 0)
        with self.assertRaises(state.StateError):
            self.store.reserve_intent(digest, 0, 43)

    def test_plan_digest_is_stable_and_tampering_fails_closed(self):
        plan = sample_plan()
        digest = self.store.create_plan(plan)
        self.assertEqual(self.store.create_plan(dict(reversed(list(plan.items())))), digest)
        returned = self.store.get_plan(digest)
        returned['payments'][0]['amount_nano'] = 99
        self.assertEqual(self.store.get_plan(digest)['payments'][0]['amount_nano'], 123456789)
        connection = sqlite3.connect(self.root / 'journal.sqlite3')
        with connection:
            connection.execute('UPDATE plans SET payload=? WHERE digest=?', (json.dumps(returned), digest))
        connection.close()
        with self.assertRaises(state.StateError):
            self.store.get_plan(digest)

    def test_invalid_plan_cannot_create_a_payment(self):
        for amount in [-1, True, 0.1, '100', 0]:
            plan = sample_plan(amount=amount)
            with self.subTest(amount=amount), self.assertRaises(state.StateError):
                self.store.create_plan(plan)
        self.assertEqual(self.store.list_intents(), [])

    def test_two_processes_cannot_hold_same_wallet_lock(self):
        code = '''import sys
from payout_state import wallet_lock, WalletLocked
try:
    with wallet_lock(sys.argv[1], sys.argv[2]): pass
except WalletLocked:
    sys.exit(23)
'''
        with state.wallet_lock(self.root, 'TEST-WALLET'):
            same = subprocess.run([sys.executable, '-c', code, str(self.root), 'TEST-WALLET'], capture_output=True)
            other = subprocess.run([sys.executable, '-c', code, str(self.root), 'OTHER-WALLET'], capture_output=True)
        self.assertEqual(same.returncode, 23, same.stderr)
        self.assertEqual(other.returncode, 0, other.stderr)
        with state.wallet_lock(self.root, 'TEST-WALLET'):
            pass

    def test_missing_or_corrupt_journal_is_not_reinitialized(self):
        with self.assertRaises(state.StateError):
            state.StateStore(Path(self.temp.name) / 'missing')
        broken = Path(self.temp.name) / 'broken'
        broken.mkdir()
        (broken / 'journal.sqlite3').write_bytes(b'corrupt')
        with self.assertRaises(state.StateError):
            state.StateStore(broken, create=True)

    def test_private_permissions(self):
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((self.root / 'journal.sqlite3').stat().st_mode & 0o777, 0o600)

    def test_verified_backup_restores_only_to_new_target(self):
        digest = self.store.create_plan(sample_plan())
        self.store.reserve_intent(digest, 0, 42)
        private_file = Path(self.temp.name) / 'ledger.json'
        private_file.write_text('{"balances":{},"history":[]}')
        backup = Path(self.temp.name) / 'backup'
        self.store.backup(backup, extra_files=[private_file])
        restored = Path(self.temp.name) / 'restored'
        state.verified_restore(backup, restored)
        self.assertEqual(state.StateStore(restored).get_plan(digest)['epoch'], 7)
        self.assertEqual((restored / 'ledger.json').read_bytes(), private_file.read_bytes())
        with self.assertRaises((state.StateError, FileExistsError)):
            state.verified_restore(backup, restored)
        (backup / 'ledger.json').write_text('damaged')
        refused = Path(self.temp.name) / 'refused'
        with self.assertRaises(state.StateError):
            state.verified_restore(backup, refused)
        self.assertFalse(refused.exists())

    def test_historical_import_never_reserves_or_rewrites_source(self):
        history = Path(self.temp.name) / 'legacy.json'
        history.write_bytes(b'{\r\n"epoch":6,"txs":["TX1"]\r\n}')
        original = history.read_bytes()
        identity = self.store.import_history(history, {'epoch': 6, 'era': 'mesa', 'network': 'testnet'})
        self.assertEqual(self.store.list_intents(), [])
        self.assertEqual(identity, hashlib.sha256(original).hexdigest())
        self.assertEqual(history.read_bytes(), original)
        self.assertEqual(state.StateStore(self.root).historical_records()[0]['digest'], identity)

    def test_same_nonce_is_independent_between_networks(self):
        first = self.store.create_plan(sample_plan())
        other = sample_plan()
        other['network'] = 'other-network'
        second = self.store.create_plan(other)
        self.store.reserve_intent(first, 0, 42)
        self.store.reserve_intent(second, 0, 42)
        self.assertEqual(self.store.pending_commitments('TEST-WALLET', network='testnet'), 133456789)

    def test_incomplete_or_wrong_chain_confirmation_cannot_close_intent(self):
        digest = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_submitted(intent['id'], 'TX-HASH')
        correct = {'tx_hash': 'TX-HASH', 'block_hash': 'BLOCK', 'depth': 20,
            'on_best_chain': True, 'status': 'applied', 'wallet': 'TEST-WALLET', 'recipient': 'TEST-RECIPIENT',
            'amount_nano': 123456789, 'fee_nano': 10000000, 'memo': 'Epoch 7',
            'nonce': 42, 'network': 'testnet', 'era': 'mesa'}
        for key, value in [('depth', 19), ('on_best_chain', False), ('tx_hash', 'OTHER'),
                           ('amount_nano', True), ('fee_nano', 0), ('wallet', 'OTHER'),
                           ('memo', 'OTHER'), ('nonce', False), ('network', 'OTHER'), ('status', 'failed'), ('status', None)]:
            proof = dict(correct, **{key: value})
            with self.subTest(key=key), self.assertRaises(state.StateError):
                self.store.mark_confirmed(intent['id'], proof)
        self.assertEqual(self.store.get_intent(intent['id'])['status'], 'submitted')

    def test_lost_receipt_cannot_resolve_as_failed_when_proof_matches_payment(self):
        digest = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_unknown(intent['id'], 'response lost')
        proof = {'kind': 'nonce_consumed', 'tx_hash': 'FOUND-ORIGINAL', 'block_hash': 'BLOCK',
            'depth': 20, 'on_best_chain': True, 'status': 'applied', 'wallet': 'TEST-WALLET',
            'recipient': 'TEST-RECIPIENT', 'amount_nano': 123456789, 'fee_nano': 10000000,
            'memo': 'Epoch 7', 'nonce': 42, 'network': 'testnet', 'era': 'mesa'}
        with self.assertRaises(state.StateError):
            self.store.resolve_failed(intent['id'], proof)
        self.assertEqual(self.store.pending_commitments('TEST-WALLET'), 133456789)

    def test_expiry_without_complete_canonical_history_is_not_terminal_proof(self):
        plan = sample_plan()
        plan['valid_until_slot'] = 1000
        digest = self.store.create_plan(plan)
        intent = self.store.reserve_intent(digest, 0, 42)
        self.store.mark_unknown(intent['id'], 'response lost')
        proof = {'kind': 'expired', 'block_hash': 'BLOCK', 'depth': 20,
            'on_best_chain': True, 'status': 'applied', 'wallet': 'TEST-WALLET', 'nonce': 42,
            'network': 'testnet', 'era': 'mesa', 'best_chain_slot': 2000}
        with self.assertRaises(state.StateError):
            self.store.resolve_failed(intent['id'], proof)

    def test_wallet_lock_creates_private_root(self):
        root = Path(self.temp.name) / 'lock-only'
        with state.wallet_lock(root, 'TEST-WALLET'):
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)

    def test_backup_never_replaces_existing_destination(self):
        destination = Path(self.temp.name) / 'existing-backup'
        destination.mkdir()
        marker = destination / 'marker'
        marker.write_bytes(b'preserve me')
        with self.assertRaises(FileExistsError):
            self.store.backup(destination)
        self.assertEqual(marker.read_bytes(), b'preserve me')

    def test_alternate_epoch_plan_cannot_reserve_after_existing_intent(self):
        for status in ('unknown', 'submitted', 'confirmed', 'failed'):
            root = Path(self.temp.name) / status
            store = state.StateStore(root, create=True)
            original = store.create_plan(sample_plan())
            intent = store.reserve_intent(original, 0, 42)
            if status == 'unknown':
                store.mark_unknown(intent['id'], 'response lost')
            elif status in ('submitted', 'confirmed'):
                store.mark_submitted(intent['id'], 'TX-HASH')
                if status == 'confirmed':
                    store.mark_confirmed(intent['id'], {'tx_hash': 'TX-HASH', 'block_hash': 'BLOCK',
                        'depth': 20, 'on_best_chain': True, 'status': 'applied',
                        'wallet': 'TEST-WALLET', 'recipient': 'TEST-RECIPIENT',
                        'amount_nano': 123456789, 'fee_nano': 10000000, 'memo': 'Epoch 7',
                        'nonce': 42, 'network': 'testnet', 'era': 'mesa'})
            else:
                store.resolve_failed(intent['id'], {'kind': 'nonce_consumed', 'tx_hash': 'OTHER',
                    'block_hash': 'BLOCK', 'depth': 20, 'on_best_chain': True, 'status': 'applied',
                    'wallet': 'TEST-WALLET', 'recipient': 'DIFFERENT', 'amount_nano': 123456789,
                    'fee_nano': 10000000, 'memo': 'Epoch 7', 'nonce': 42,
                    'network': 'testnet', 'era': 'mesa'})
            changed = sample_plan()
            changed['input_digest'] = 'NEW-INPUT'
            replacement = store.create_plan(changed)
            with self.subTest(status=status), self.assertRaises(state.StateError):
                store.reserve_intent(replacement, 0, 43)

    def test_unknown_wallet_transaction_blocks_even_another_epoch(self):
        first = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(first, 0, 42)
        self.store.mark_unknown(intent['id'], 'response lost')
        next_epoch = self.store.create_plan(sample_plan(epoch=8))
        with self.assertRaises(state.StateError):
            self.store.reserve_intent(next_epoch, 0, 43)

    def test_distinct_rows_of_one_plan_can_be_reserved(self):
        plan = sample_plan()
        plan['payments'].append({'recipient': 'SECOND', 'amount_nano': 100, 'fee_nano': 10, 'memo': ''})
        digest = self.store.create_plan(plan)
        self.store.reserve_intent(digest, 0, 42)
        self.store.reserve_intent(digest, 1, 43)
        self.assertEqual(len(self.store.list_intents()), 2)

    def test_competing_restore_directory_is_preserved(self):
        backup = Path(self.temp.name) / 'backup-race'
        self.store.backup(backup)
        destination = Path(self.temp.name) / 'restore-race'
        marker = destination / 'journal.sqlite3'
        original_mkdir = Path.mkdir
        def competing_mkdir(path, *args, **kwargs):
            if path == destination and not destination.exists():
                original_mkdir(destination)
                marker.write_bytes(b'valuable competing journal')
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', autospec=True, side_effect=competing_mkdir):
            with self.assertRaises(FileExistsError):
                state.verified_restore(backup, destination)
        self.assertEqual(marker.read_bytes(), b'valuable competing journal')

    def test_competing_backup_directory_is_preserved(self):
        destination = Path(self.temp.name) / 'backup-race'
        marker = destination / 'journal.sqlite3'
        original_mkdir = Path.mkdir
        def competing_mkdir(path, *args, **kwargs):
            if path == destination and not destination.exists():
                original_mkdir(destination)
                marker.write_bytes(b'valuable competing journal')
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', autospec=True, side_effect=competing_mkdir):
            with self.assertRaises(FileExistsError):
                self.store.backup(destination)
        self.assertEqual(marker.read_bytes(), b'valuable competing journal')

    def test_withdrawal_has_real_era_and_explicit_operation_identity(self):
        payout = self.store.create_plan(sample_plan())
        intent = self.store.reserve_intent(payout, 0, 42)
        withdrawal = sample_plan()
        withdrawal.update(kind='withdrawal', operation_id='12345678-1234-1234-1234-123456789abc')
        digest = self.store.create_plan(withdrawal)
        with self.assertRaises(state.StateError):
            self.store.reserve_intent(digest, 0, 43)
        self.store.mark_submitted(intent['id'], 'TX-HASH')
        self.store.reserve_intent(digest, 0, 43)
        changed = dict(withdrawal, input_digest='CHANGED')
        changed_digest = self.store.create_plan(changed)
        with self.assertRaises(state.StateError):
            self.store.reserve_intent(changed_digest, 0, 44)
        self.assertEqual(self.store.get_plan(digest)['era'], 'mesa')

    def test_withdrawal_operation_id_and_kind_are_validated(self):
        for extra in [{'kind': 'withdrawal'}, {'kind': 'withdrawal', 'operation_id': '../unsafe'},
                      {'kind': 'anything'}]:
            with self.subTest(extra=extra), self.assertRaises(state.StateError):
                self.store.create_plan(dict(sample_plan(), **extra))
