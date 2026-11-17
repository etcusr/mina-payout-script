import multiprocessing
import os
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from batch_accounting import BatchStore, FILENAME, canonical_plan, readonly_snapshot, verify_history
from payout_state import StateError, StateStore, WalletLocked, wallet_lock
from tests.test_calculation import A, B, W, address


def fixture(operation='test', **updates):
    plan = {'wallet': W, 'network': 'chain', 'era': '3', 'operation': operation,
            'memo': 'batch_test', 'fee_nano': 7, 'batch_size': 2,
            'recipients': [{'recipient': target, 'amount_nano': value} for target, value in zip((A, B, address(4)), (1, 2, 3))]}
    plan.update(updates)
    return plan


def try_claim(state_dir, digest, queue):
    queue.put(BatchStore(state_dir, create=False).claim(digest, 0))


def try_lock(state_dir, queue):
    try:
        with wallet_lock(state_dir, W): queue.put('acquired')
    except WalletLocked:
        queue.put('locked')


class BatchAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / '.payout-state'
        self.store = BatchStore(self.root)
        self.digest = self.store.create_plan(fixture())

    def prepare(self, index=0, nonce=10):
        self.store.prepare(self.digest, index, nonce, {'signed': 'packet-' + str(index)}, 'hash-' + str(index))

    def settle(self, index=0, status='applied', depth=3):
        self.prepare(index=index, nonce=10 + index)
        self.store.claim(self.digest, index)
        proof = {**self.store.expected(self.digest, index), 'status': status,
                 'on_best_chain': True, 'depth': depth, 'block_hash': 'block', 'height': 12}
        self.store.observe(self.digest, index, proof)
        return proof

    def test_plan_is_canonical_and_split_deterministically(self):
        plan = self.store.get_plan(self.digest)
        self.assertEqual(plan['batches'], [{'index': 0, 'recipients': fixture()['recipients'][:2]},
                                           {'index': 1, 'recipients': fixture()['recipients'][2:]}])
        self.assertEqual(self.digest, self.store.create_plan(dict(reversed(list(fixture().items())))))
        self.assertEqual(self.store.snapshot(W, 'chain')['reserved_nano'], 20)
        self.assertEqual(os.stat(self.store.path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.root).st_mode & 0o777, 0o700)

    def test_exact_integer_fields_reject_bool_float_and_string(self):
        for key in ('fee_nano', 'batch_size'):
            for value in (True, 1.0, '1', -1):
                with self.subTest(key=key, value=value), self.assertRaises(StateError):
                    canonical_plan(fixture(**{key: value}))
        for value in (True, 1.0, '1', 0):
            with self.subTest(value=value), self.assertRaises(StateError):
                canonical_plan(fixture(recipients=[{'recipient': A, 'amount_nano': value}]))
        for extra in ({'epoch': 9}, {'batches': []}, {'memo': 'ж' * 17}):
            with self.assertRaises(StateError): canonical_plan(fixture(**extra))

    def test_immutable_operation_and_one_unfinished_per_wallet(self):
        with self.assertRaisesRegex(StateError, 'immutable'):
            self.store.create_plan(fixture(fee_nano=8))
        with self.assertRaisesRegex(StateError, 'unfinished'):
            self.store.create_plan(fixture(operation='second'))
        with self.assertRaisesRegex(StateError, 'unfinished'):
            self.store.create_plan(fixture(operation='second', network='other'))

    def test_claim_is_durable_unknown_and_restart_cannot_resubmit(self):
        self.prepare()
        self.assertTrue(self.store.claim(self.digest, 0))
        restarted = BatchStore(self.root, create=False)
        self.assertEqual(restarted.rows(self.digest)[0]['status'], 'unknown')
        self.assertFalse(restarted.claim(self.digest, 0))
        restarted.prepare(self.digest, 0, 10, {'signed': 'packet-0'}, 'hash-0')
        self.assertFalse(restarted.claim(self.digest, 0))
        self.assertEqual(restarted.snapshot(W, 'chain')['reserved_nano'], 20)
        self.assertEqual(restarted.snapshot(W, 'chain')['known_nonces'], [10])
        with self.assertRaises(StateError): restarted.prepare(self.digest, 0, 11, {'signed': 'new'}, 'new')

    def test_two_processes_only_one_claim(self):
        self.prepare()
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        children = [context.Process(target=try_claim, args=(str(self.root), self.digest, queue)) for _ in range(2)]
        for child in children: child.start()
        for child in children:
            child.join(15)
            self.assertEqual(child.exitcode, 0)
        self.assertEqual(sorted([queue.get(timeout=2), queue.get(timeout=2)]), [False, True])
        queue.close()

    def test_shared_ordinary_wallet_lock_blocks_batch_process(self):
        context = multiprocessing.get_context('spawn')
        queue = context.Queue()
        with wallet_lock(self.root, W):
            child = context.Process(target=try_lock, args=(str(self.root), queue))
            child.start(); child.join(15)
            self.assertEqual(child.exitcode, 0)
            self.assertEqual(queue.get(timeout=2), 'locked')
        queue.close()

    def test_unclaimed_and_out_of_order_batches_cannot_settle_or_prepare(self):
        self.assertFalse(self.store.claim(self.digest, 0))
        with self.assertRaises(StateError): self.prepare(index=1, nonce=11)
        self.prepare()
        proof = {**self.store.expected(self.digest, 0), 'status': 'applied', 'on_best_chain': True, 'depth': 3}
        with self.assertRaises(StateError): self.store.observe(self.digest, 0, proof)

    def test_fee_and_recipient_cost_conserved_after_failure_and_success(self):
        proof = self.settle(status='failed')
        state = self.store.snapshot(W, 'chain')
        self.assertEqual((state['spent_nano'], state['reserved_nano']), (7, 10))
        self.assertFalse(self.store.observe(self.digest, 0, proof))
        self.settle(index=1)
        state = self.store.snapshot(W, 'chain')
        self.assertEqual((state['spent_nano'], state['reserved_nano'], state['known_nonces'], state['unfinished']), (17, 0, [10, 11], []))
        self.assertFalse(self.store.claim(self.digest, 0))
        self.assertNotEqual(self.store.create_plan(fixture(operation='second')), self.digest)

    def test_terminal_status_cannot_be_changed(self):
        proof = self.settle(status='failed')
        with self.assertRaises(StateError): self.store.observe(self.digest, 0, dict(proof, status='applied'))

    def test_reserve_advance_is_explicit_bounded_and_immutable(self):
        plan=canonical_plan(fixture(reserve_advance_nano=20))
        self.assertEqual(plan['reserve_advance_nano'],20)
        for amount in (-1,True,'20',21):
            with self.assertRaises(StateError):canonical_plan(fixture(reserve_advance_nano=amount))
        with self.assertRaisesRegex(StateError,'immutable'):
            self.store.create_plan(plan)

    def test_cancellation_requires_failure_and_never_releases_signed_packet(self):
        with self.assertRaises(StateError): self.store.cancel_unsigned_after_failure(self.digest)
        self.settle(status='failed')
        self.prepare(index=1,nonce=11)
        self.store.cancel_unsigned_after_failure(self.digest)
        self.assertEqual(self.store.rows(self.digest)[1]['status'],'prepared')
        self.assertEqual(self.store.snapshot(W,'chain')['reserved_nano'],10)

    def test_cancelled_unsigned_suffix_is_terminal_for_ordinary_history_guard(self):
        self.settle(status='failed');self.store.cancel_unsigned_after_failure(self.digest)
        with patch('batch_archive.verify',return_value={**self.store.expected(self.digest,0),
                'status':'failed','on_best_chain':True,'depth':15}) as verify:
            verify_history(self.root,{},W,'chain',required_depth=15)
        self.assertEqual(verify.call_count,1)
        self.assertEqual(self.store.snapshot(W,'chain')['reserved_nano'],0)
        self.assertNotEqual(self.store.create_plan(fixture(operation='next')),self.digest)

    def test_nonce_cannot_be_reused_for_another_batch(self):
        self.settle()
        with self.assertRaisesRegex(StateError, 'nonce'):
            self.prepare(index=1, nonce=10)

    def test_rejects_wrong_identity_depth_and_orphan_proofs(self):
        self.prepare(); self.store.claim(self.digest, 0)
        proof = {**self.store.expected(self.digest, 0), 'status': 'applied', 'on_best_chain': True, 'depth': 3}
        changes = ({'network': 'wrong'}, {'era': '4'}, {'wallet': A}, {'nonce': 11}, {'tx_hash': 'other'},
                   {'fee_nano': 8}, {'nonce': True}, {'fee_nano': True}, {'memo': ''}, {'recipients': []}, {'depth': 2}, {'depth': True}, {'on_best_chain': False})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(StateError):
                self.store.observe(self.digest, 0, dict(proof, **change))
        self.assertFalse(self.store.observe(self.digest, 0, {'status': 'pending'}))
        self.assertEqual(self.store.rows(self.digest)[0]['status'], 'unknown')

    def test_readonly_snapshot_never_creates_or_changes_any_file(self):
        absent = Path(self.temp.name) / 'absent'
        self.assertEqual(readonly_snapshot(absent, W, 'chain')['reserved_nano'], 0)
        self.assertFalse(absent.exists())
        self.prepare(); self.store.claim(self.digest, 0)
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        readonly_snapshot(self.root, W, 'chain')
        self.store.list_plans(); self.store.get_plan(self.digest); self.store.rows(self.digest)
        after = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_corrupt_signed_packet_is_not_claimable(self):
        self.prepare()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE batches SET signed='{}' WHERE batch_index=0"); db.commit()
        with self.assertRaisesRegex(StateError, 'checksum'): self.store.claim(self.digest, 0)

    def test_corrupt_plan_is_not_readable(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute("UPDATE plans SET payload='{}'"); db.commit()
        with self.assertRaises(StateError): self.store.snapshot(W, 'chain')

    def test_fresh_history_read_does_not_mutate_ledger_and_rejects_reorg(self):
        self.settle(); self.settle(index=1, status='failed')
        before = self.store.path.read_bytes()
        def verify(config, expected, required_depth):
            return {**expected, 'status': 'applied' if expected['nonce'] == 10 else 'failed',
                    'on_best_chain': True, 'depth': 15}
        with patch('batch_archive.verify', side_effect=verify) as checker:
            verify_history(self.root, {}, W, 'chain', required_depth=15)
            self.assertEqual(checker.call_count, 2)
        self.assertEqual(before, self.store.path.read_bytes())
        with patch('batch_archive.verify', return_value={'status': 'unknown'}):
            with self.assertRaises(StateError): verify_history(self.root, {}, W, 'chain')

    def test_pending_batch_blocks_ordinary_proof_guard(self):
        with self.assertRaisesRegex(StateError, 'пакетная'):
            verify_history(self.root, {}, W, 'chain')

    def test_owner_income_debits_once_and_known_nonces_do_not_hide_missing_nonce(self):
        import owner_income
        normal = StateStore(self.root, create=True)
        owner_income.initialize_account(normal, W, 'chain', 10)
        owner_income.record_credit(normal, {'wallet': W, 'network': 'chain', 'era': '3', 'epoch': 1,
                                  'validator_nano': 100, 'retained_own_nano': 0, 'payout_fees_nano': 0,
                                  'own_paid_nano': 0})
        before = normal.path.read_bytes()
        self.assertEqual(owner_income.summary(normal, W, 'chain')['available_nano'], 80)
        self.settle(status='failed'); self.settle(index=1)
        account = owner_income.summary(normal, W, 'chain')
        self.assertEqual(account['available_nano'], 83)
        self.assertEqual(account['withdrawn_with_fees_nano'], 17)
        owner_income.check_external_spends(normal, W, 'chain', {'account': {'nonce': 12}}, account)
        with self.assertRaises(StateError):
            owner_income.check_external_spends(normal, W, 'chain', {'account': {'nonce': 13}}, account)
        self.assertEqual(before, normal.path.read_bytes())

    def test_liquid_reserve_includes_batch_exactly_once(self):
        from payout_workflow import Workflow
        from tests.test_workflow import Fake
        normal = StateStore(self.root, create=True)
        flow = Workflow({'SEND_FROM_ADDRESS': W, 'EXPECTED_CHAIN_ID': 'chain'}, self.temp.name, adapter=Fake())
        funds = flow._available_funds(normal, {'account': {'liquid_nano': 100}, 'mempool_pending_nano': 0, 'pooled_commands': []}, 13)
        self.assertEqual((funds['commitments_nano'], funds['available_nano']), (20, 67))

    def test_ordinary_previous_guard_checks_batches_even_without_ordinary_history(self):
        from payout_workflow import Workflow
        from tests.test_workflow import Fake
        normal = StateStore(self.root, create=True)
        flow = Workflow({'SEND_FROM_ADDRESS': W, 'EXPECTED_CHAIN_ID': 'chain'}, self.temp.name, adapter=Fake())
        with self.assertRaisesRegex(StateError, 'пакетная'):
            flow._verify_previous(normal, {})

    def test_rejects_unpayable_plans_before_reserving(self):
        invalid = ({'batch_size': 31}, {'fee_nano': 0}, {'fee_nano': 2**64},
                   {'operation': '../bad'}, {'operation': 'тест'}, {'wallet': 'invalid'},
                   {'recipients': [{'recipient': W, 'amount_nano': 1}]},
                   {'recipients': [{'recipient': A, 'amount_nano': 1}] * 2},
                   {'recipients': [{'recipient': A, 'amount_nano': 2**64 - 1}]})
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(StateError):
                canonical_plan(fixture(**change))

    def test_backup_restore_preserves_batch_claim_and_owner_debit(self):
        from payout_state import verified_restore
        normal = StateStore(self.root, create=True)
        self.prepare(); self.store.claim(self.digest, 0)
        before = normal.path.read_bytes()
        destination = Path(self.temp.name) / 'backup'
        with wallet_lock(self.root, W): normal.backup(destination)
        self.assertTrue((destination / FILENAME).exists())
        restored = verified_restore(destination, Path(self.temp.name) / 'restored')
        recovered = BatchStore(restored, create=False)
        self.assertEqual(recovered.rows(self.digest)[0]['status'], 'unknown')
        self.assertFalse(recovered.claim(self.digest, 0))
        self.assertEqual(recovered.snapshot(W, 'chain')['reserved_nano'], 20)
        self.assertEqual(before, normal.path.read_bytes())

    def test_broken_ledger_symlink_is_not_treated_as_absent(self):
        missing = Path(self.temp.name) / 'linked'
        missing.mkdir()
        (missing / FILENAME).symlink_to(missing / 'not-present')
        with self.assertRaises(StateError): readonly_snapshot(missing, W, 'chain')

    def test_intent_checksum_binds_nonce_and_transaction_hash(self):
        self.prepare()
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('UPDATE batches SET nonce=99 WHERE batch_index=0'); db.commit()
        with self.assertRaisesRegex(StateError, 'checksum'): self.store.claim(self.digest, 0)

    def test_ordinary_history_batch_bypass_is_explicit(self):
        from payout_workflow import Workflow
        from tests.test_workflow import Fake
        normal = StateStore(self.root, create=True)
        flow = Workflow({'SEND_FROM_ADDRESS': W, 'EXPECTED_CHAIN_ID': 'chain'}, self.temp.name, adapter=Fake())
        flow._verify_previous(normal, {}, verify_batches=False)

    def test_ordinary_history_enforces_three_and_fifteen_block_depths(self):
        from payout_workflow import Workflow
        from tests.test_workflow import Fake
        normal = StateStore(self.root, create=True)
        flow = Workflow({'SEND_FROM_ADDRESS': W, 'EXPECTED_CHAIN_ID': 'chain',
                         'TX_CONFIRMATIONS_NUM': 1, 'PREVIOUS_PAYOUT_CONFIRMATIONS_NUM': 1},
                        self.temp.name, adapter=Fake())
        with patch('batch_accounting.verify_history') as checker:
            flow._verify_previous(normal, {})
            self.assertEqual(checker.call_args.kwargs['required_depth'], 3)
            flow._verify_previous(normal, {}, next_epoch=10)
            self.assertEqual(checker.call_args.kwargs['required_depth'], 15)
