"""Linked epoch batches retain one financial record across two journals."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from batch_accounting import BatchStore, canonical_plan, verify_history
from payout_state import StateError, StateStore, _canonical, _hash
from tests.test_calculation import A, B, W, address
import owner_income


def epoch_plan(**updates):
    recipients = sorted((A, B, address(4)))
    plan = {'wallet': W, 'network': 'chain', 'era': '3', 'epoch': 9,
            'policy': {'transport': 'zkapp_batch', 'batch_size': 2, 'fee_nano': 7,
                       'network_fee_source': 'validator'},
            'payments': [{'recipient': a, 'amount_nano': amount,
                          'fee_nano': fee, 'memo': 'epoch_9'}
                         for a, amount, fee in zip(recipients, (10, 20, 30), (4, 3, 7))],
            'calculation': {'validator_nano': 100, 'excluded_nano': 0,
                            'total_reward_nano': 160, 'excluded_by_address_nano': {}}}
    plan.update(updates)
    return plan


class EpochBatchStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'state'
        self.store = StateStore(self.root, create=True)
        self.plan = epoch_plan()
        self.digest = self.store.create_plan(self.plan)

    def link(self):
        from payout_batch_state import batch_plan
        self.batches = BatchStore(self.root)
        self.batch_digest = self.batches.create_plan(batch_plan(self.plan, self.digest))
        self.store.bind_batch(self.digest, self.batch_digest, 10)
        return self.batch_digest

    def resolve(self, index=0, status='applied'):
        self.batches.prepare(self.batch_digest, index, 10 + index,
                             {'signed': 'packet-' + str(index)}, 'hash-' + str(index))
        self.batches.claim(self.batch_digest, index)
        proof = dict(self.batches.expected(self.batch_digest, index), status=status,
                     on_best_chain=True, depth=3, block_hash='block')
        self.batches.observe(self.batch_digest, index, proof)

    def test_batch_plan_cannot_reserve_ordinary_nonce(self):
        with self.assertRaisesRegex(StateError, 'batch'):
            self.store.reserve_intent(self.digest, 0, 10)
        self.assertEqual(self.store.list_intents(wallet=W), [])

    def test_missing_batch_ledger_cannot_settle_nonempty_epoch(self):
        self.assertEqual(self.store.list_intents(plan_digest=self.digest), [])
        with self.assertRaises(StateError):
            self.store.settle(self.digest, {})
        self.assertFalse((self.root / 'batch-mainnet.sqlite3').exists())

    def test_batch_link_marker_is_immutable_and_bound_to_both_plans(self):
        self.link()
        marker = self.store.batch_link(self.digest)
        self.assertEqual(marker, {'payout_plan': self.digest, 'batch_plan': self.batch_digest,
                                 'initial_nonce': 10, 'wallet': W, 'network': 'chain', 'era': '3'})
        self.assertEqual(self.store.bind_batch(self.digest, self.batch_digest, 10), marker)
        for digest, nonce in ((self.batch_digest, 11), ('f' * 64, 10)):
            with self.assertRaises(StateError): self.store.bind_batch(self.digest, digest, nonce)

    def test_missing_batch_journal_after_send_cannot_recreate_unsettled_epoch(self):
        self.link(); self.resolve()
        self.batches.path.unlink()
        with self.assertRaises(StateError): self.store.list_intents(plan_digest=self.digest)
        with self.assertRaises(StateError):
            verify_history(self.root, {}, W, 'chain', exclude=self.digest)
        from payout_batch_state import batch_plan
        with self.assertRaises(StateError):
            BatchStore(self.root).create_plan(batch_plan(self.plan, self.digest))

    def test_started_linked_rows_require_ordinary_marker(self):
        self.link(); self.resolve()
        with self.store._connection() as db:
            db.execute("DELETE FROM historical WHERE source_name=?", ('batch_link:' + self.digest,))
        with self.assertRaises(StateError): self.batches.rows(self.batch_digest)
        with self.assertRaises(StateError): self.store.list_intents(plan_digest=self.digest)

    def test_linked_prepare_requires_marker_and_its_exact_nonce(self):
        self.link()
        with self.assertRaises(StateError):
            self.batches.prepare(self.batch_digest, 0, 11, {'signed': 'packet'}, 'hash')
        with self.store._connection() as db:
            db.execute("DELETE FROM historical WHERE source_name=?", ('batch_link:' + self.digest,))
        with self.assertRaises(StateError):
            self.batches.prepare(self.batch_digest, 0, 10, {'signed': 'packet'}, 'hash')
        self.assertEqual(self.batches.rows(self.batch_digest)[0]['status'], 'planned')
        self.store.bind_batch(self.digest, self.batch_digest, 10)
        self.batches.prepare(self.batch_digest, 0, 10, {'signed': 'packet'}, 'hash')
        self.assertEqual(self.batches.rows(self.batch_digest)[0]['status'], 'prepared')

    def test_deterministic_plan_and_fee_shares_preserve_epoch(self):
        self.link()
        linked = self.batches.get_plan(self.batch_digest)
        self.assertEqual(linked['payout_plan'], self.digest)
        self.assertEqual(linked['operation'], 'payout_' + self.digest)
        self.assertEqual(linked['fee_nano'], 7)
        self.assertEqual([len(b['recipients']) for b in linked['batches']], [2, 1])
        self.assertEqual(self.batches.snapshot(W, 'chain')['reserved_nano'], 74)
        self.assertEqual(self.store.list_intents(plan_digest=self.digest), [])
        self.assertEqual(self.batch_digest, self.batches.create_plan(linked))

    def test_projection_does_not_duplicate_global_nonces(self):
        self.link()
        self.batches.prepare(self.batch_digest, 0, 10, {'signed': 'packet'}, 'hash')
        intents = self.store.list_intents(plan_digest=self.digest)
        self.assertEqual([i['status'] for i in intents], ['unknown', 'unknown'])
        self.assertEqual([i['payment_index'] for i in intents], [0, 1])
        self.assertEqual([i['nonce'] for i in intents], [10, 10])
        self.assertEqual(len({i['id'] for i in intents}), 2)
        self.assertTrue(all(isinstance(i['id'], str) for i in intents))
        self.assertEqual(self.store.list_intents(wallet=W), [])
        self.assertEqual(self.store.list_intents(plan_digest=self.digest, wallet=A), [])

    def test_linked_costs_are_wallet_reserves_but_never_owner_withdrawals(self):
        self.link()
        income = owner_income.summary(self.store, W, 'chain')
        self.assertEqual(income['reserved_with_fees_nano'], 0)
        self.assertEqual(self.batches.snapshot(W, 'chain')['reserved_nano'], 74)
        self.resolve(); self.resolve(1)
        self.store.settle(self.digest, {})
        intents = self.store.list_intents(plan_digest=self.digest)
        owner_income.record_payout(self.store, self.digest, self.plan, intents,
                                   {'SEND_FROM_ADDRESS': W}, 10)
        income = owner_income.summary(self.store, W, 'chain')
        self.assertEqual(income['earned_nano'], 86)
        self.assertEqual(income['available_nano'], 86)
        self.assertEqual(income['withdrawn_with_fees_nano'], 0)
        self.assertEqual(self.batches.snapshot(W, 'chain')['spent_nano'], 74)
        owner_income.check_external_spends(self.store, W, 'chain',
                                          {'account': {'nonce': 12}}, income)

    def test_failed_fee_is_charged_and_cancelled_suffix_has_no_fee(self):
        self.link(); self.resolve(status='failed')
        self.batches.cancel_unsigned_after_failure(self.batch_digest)
        intents = self.store.list_intents(plan_digest=self.digest)
        self.assertEqual([i['status'] for i in intents], ['failed'] * 3)
        self.assertEqual([i['payment']['fee_nano'] for i in intents], [4, 3, 0])
        self.assertEqual(intents[-1]['evidence']['kind'], 'zkapp_batch_cancelled')
        self.store.settle(self.digest, {})
        owner_income.record_payout(self.store, self.digest, self.plan, intents,
                                   {'SEND_FROM_ADDRESS': W, 'WITHDRAW_TO_ADDRESS': A}, 10)
        income = owner_income.summary(self.store, W, 'chain')
        self.assertEqual(income['available_nano'], 93)
        self.assertEqual(income['credits'][0]['own_paid_nano'], 0)
        self.assertEqual(self.batches.snapshot(W, 'chain')['spent_nano'], 7)

    def test_linked_failed_batch_never_authorizes_signing_a_later_batch(self):
        self.link(); self.resolve(status='failed')
        with self.assertRaises(StateError):
            self.batches.prepare(self.batch_digest, 1, 11, {'signed': 'packet'}, 'hash-next')
        self.assertEqual(self.batches.rows(self.batch_digest)[1]['status'], 'planned')

    def test_forged_or_missing_ordinary_link_never_suppresses_owner_debit(self):
        self.link()
        linked = self.batches.get_plan(self.batch_digest)
        with self.assertRaises(StateError):
            canonical_plan(dict(linked, reserve_advance_nano=1))
        for key, value in (('fee_nano', 8), ('payout_plan', 'f' * 64)):
            with self.subTest(key=key):
                forged = dict(linked, **{key: value})
                forged['operation'] = 'forged_' + key
                payload = _canonical(forged)
                digest = _hash(payload.encode())
                with self.batches._connection(write=True) as db:
                    db.execute('INSERT INTO plans VALUES (?,?,?,?,?)',
                               (digest, W, 'chain', 'forged_' + key, payload))
                with self.assertRaises(StateError):
                    owner_income.summary(self.store, W, 'chain')
                with self.batches._connection(write=True) as db:
                    db.execute('DELETE FROM plans WHERE digest=?', (digest,))
        with self.store._connection() as db:
            db.execute('DELETE FROM plans WHERE digest=?', (self.digest,))
        for reader in (lambda: self.batches.get_plan(self.batch_digest),
                       lambda: self.batches.rows(self.batch_digest),
                       lambda: self.batches.snapshot(W, 'chain'),
                       lambda: owner_income.summary(self.store, W, 'chain')):
            with self.assertRaises(StateError): reader()

    def test_tampered_terminal_proof_cannot_project_or_settle(self):
        self.link(); self.resolve(); self.resolve(1)
        with self.batches._connection(write=True) as db:
            db.execute("UPDATE batches SET proof='{}' WHERE batch_index=0")
        with self.assertRaises(StateError): self.store.list_intents(plan_digest=self.digest)
        with self.assertRaises(StateError): self.store.settle(self.digest, {})

    def test_cancelled_without_proven_earlier_failure_cannot_release_reserves(self):
        self.link()
        with self.batches._connection(write=True) as db:
            db.execute("UPDATE batches SET status='cancelled'")
        with self.assertRaises(StateError): self.store.list_intents(plan_digest=self.digest)
        with self.assertRaises(StateError): self.store.settle(self.digest, {})

    def test_deleted_linked_journal_cannot_erase_settled_payment_history(self):
        self.link(); self.resolve(); self.resolve(1)
        self.store.settle(self.digest, {})
        self.batches.path.unlink()
        with self.assertRaises(StateError): self.store.list_intents(plan_digest=self.digest)
        with self.assertRaises(StateError): verify_history(self.root, {}, W, 'chain')

    def test_missing_linked_journal_blocks_other_operations_until_epoch_settlement(self):
        with self.assertRaises(StateError): verify_history(self.root, {}, W, 'chain')
        verify_history(self.root, {}, W, 'chain', exclude=self.digest)
        self.assertFalse((self.root / 'batch-mainnet.sqlite3').exists())

    def test_failed_delegator_funded_fee_does_not_reduce_owner_income(self):
        self.plan['policy']['network_fee_source'] = 'delegators'
        self.digest = self.store.create_plan(self.plan)
        self.link(); self.resolve(status='failed')
        self.batches.cancel_unsigned_after_failure(self.batch_digest)
        self.store.settle(self.digest, {})
        owner_income.record_payout(self.store, self.digest, self.plan,
                                   self.store.list_intents(plan_digest=self.digest),
                                   {'SEND_FROM_ADDRESS': W}, 10)
        income = owner_income.summary(self.store, W, 'chain')
        self.assertEqual(income['available_nano'], 100)
        self.assertEqual(income['credits'][0]['delegator_fees_nano'], 7)
        self.assertEqual(income['credits'][0]['payout_fees_nano'], 0)

    def test_invalid_transport_and_noncanonical_fee_policy_are_not_journaled(self):
        for change in ({'transport': 'future'}, {'batch_size': 0}, {'batch_size': 31},
                       {'fee_nano': True}, {'fee_nano': 8}, {'reserve_advance_nano': 1}):
            plan = copy.deepcopy(self.plan)
            plan['policy'].update(change)
            with self.subTest(change=change), self.assertRaises(StateError):
                self.store.create_plan(plan)
        for target in (W, 'invalid-address'):
            plan = epoch_plan(payments=[{'recipient': target, 'amount_nano': 1,
                                        'fee_nano': 7, 'memo': 'epoch_9'}])
            with self.subTest(target=target), self.assertRaises(StateError):
                self.store.create_plan(plan)
        for field in ('recipient', 'memo', 'fee_nano'):
            plan = copy.deepcopy(self.plan)
            plan['payments'][1][field] = 'different' if field == 'memo' else plan['payments'][0][field]
            with self.subTest(field=field), self.assertRaises(StateError):
                self.store.create_plan(plan)

    def test_linked_reads_do_not_mutate_either_journal(self):
        self.link(); self.resolve()
        before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.store.list_intents(plan_digest=self.digest)
        self.batches.snapshot(W, 'chain')
        owner_income.summary(self.store, W, 'chain')
        after = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.assertEqual(before, after)

    def test_history_exclusion_is_exact_and_settlement_is_required(self):
        self.link()
        verify_history(self.root, {}, W, 'chain', exclude=self.digest)
        verify_history(self.root, {}, W, 'chain', exclude=self.batch_digest)
        with self.assertRaises(StateError): verify_history(self.root, {}, W, 'chain', exclude='other')
        self.resolve(); self.resolve(1)
        with self.assertRaisesRegex(StateError, 'settled|сверен'):
            verify_history(self.root, {}, W, 'chain')
        self.store.settle(self.digest, {})
        with patch('batch_archive.verify', side_effect=lambda config, expected, required_depth:
                   dict(expected, status='applied', on_best_chain=True, depth=required_depth)):
            verify_history(self.root, {}, W, 'chain', required_depth=15)
        with patch('batch_archive.verify', return_value={'status': 'unknown'}):
            with self.assertRaises(StateError): verify_history(self.root, {}, W, 'chain')

    def test_old_plans_keep_ordinary_intents(self):
        legacy = epoch_plan(epoch=8, policy={})
        digest = self.store.create_plan(legacy)
        intent = self.store.reserve_intent(digest, 0, 10)
        self.assertIsInstance(intent['id'], int)
        self.assertEqual(self.store.list_intents(wallet=W), [intent])
