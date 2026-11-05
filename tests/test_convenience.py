"""User entrypoints exercise real journal/accounting with fake network only."""
import contextlib
import io
import importlib.util
from pathlib import Path
import runpy
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml

from payout_state import StateStore
from payout_workflow import Workflow
from tests.test_calculation import A, W
from tests.test_workflow import Fake


class ConvenienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.fake = Fake()
        self.config = {
            'SEND_FROM_ADDRESS': W, 'VALIDATOR_ADDRESS': W,
            'WITHDRAW_TO_ADDRESS': A, 'EXPECTED_CHAIN_ID': 'chain',
            'LEGACY_PROTOCOL_VERSION_ID': 3, 'DEFAULT_TX_FEE': 1,
            'TX_CHECK_TIMER_SECONDS': 0, 'AUTO_SETTLE_WAIT_SECONDS': 0,
            'TX_CONFIRMATIONS_NUM': 20,  # Retain coverage for explicit legacy policy.
            'ARCHIVE_DB_URL': 'postgresql://reader@localhost/test',
        }
        self.config_path = self.root / 'config.yml'
        self.config_path.write_text(yaml.safe_dump(self.config))
        self.workflow = Workflow(self.config, self.root, adapter=self.fake)
        self.workflow.initialize()
        self.include = False
        old_health = self.fake.health

        def health(*args, **kwargs):
            if self.include and self.fake.pool:
                self.fake.include()
            return old_health(*args, **kwargs)

        self.fake.health = health

    def plan(self, empty=False):
        return self.workflow.create_plan(8, {
            'gross_nano': {A: 7 if empty else 100},
            'net_nano': {} if empty else {A: 100}, 'carry_before_nano': {},
            'carry_after_nano': {A: 7} if empty else {}, 'policy': {'fee_nano': 1},
        }, {})

    def run_entry(self, name, *args, wait='0', include_context=True):
        import payout_cli
        script = Path(__file__).resolve().parents[1] / name
        output, error = io.StringIO(), io.StringIO()
        argv = [name, *args]
        if include_context:
            argv += ['--project-root', str(self.root), '--config', str(self.config_path)]
        argv += ['--yes', '--wait-seconds', wait]
        original_load = payout_cli.load_config

        def fixture_only(path=None):
            # A broken continuation must never open the real project config.
            self.assertIsNotNone(path)
            self.assertEqual(Path(path).resolve(), self.config_path.resolve())
            return original_load(path)

        with patch.object(sys, 'argv', argv), \
                patch.object(payout_cli, 'load_config', side_effect=fixture_only), \
                patch('payout_workflow.LiveAdapter', return_value=self.fake), \
                patch.object(payout_cli.tunnel, 'ensure', return_value={}), \
                patch('getpass.getpass', return_value='fake-password'), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            try:
                runpy.run_path(str(script), run_name='__main__')
            except SystemExit as result:
                return result.code, output.getvalue(), error.getvalue()
        self.fail('CLI must exit with a status')

    def sent_count(self):
        return sum(isinstance(call, tuple) for call in self.fake.calls)

    def test_auto_withdraw_entrypoint_computes_amount_and_repeat_does_not_send(self):
        import owner_income
        store=self.workflow.store()
        owner_income.initialize_account(store,W,'chain',self.fake.nonce)
        owner_income.record_credit(store,{'wallet':W,'network':'chain','era':'3','epoch':6,
            'validator_nano':100,'retained_own_nano':20,'payout_fees_nano':2,'own_paid_nano':500,'source':{'kind':'fixture'}})
        self.include=True
        code,out,error=self.run_entry('withdraw.py')
        self.assertEqual(code,0,error)
        self.assertIn('0.000000117',out)
        self.assertIn('Доход владельца',out)
        final=out.split('Итоговый учёт · данные БД')[-1]
        self.assertIn('Итоговый учёт · данные БД',out)
        self.assertLess(out.index('Сверка завершена'),out.index('Итоговый учёт · данные БД'))
        self.assertRegex(final,r'Выведено \(с комиссиями\)\s+0\.000000118')
        self.assertRegex(final,r'Осталось \(до комиссии вывода\)\s+0\.000000000')
        self.assertRegex(final,r'Зарезервировано \(с комиссиями\)\s+0\.000000000')
        code,out,error=self.run_entry('withdraw.py')
        self.assertEqual(code,0,error)
        self.assertIn('Новых средств для вывода нет',out)
        self.assertIn('Итоговый учёт · данные БД',out)
        self.assertRegex(out.split('Итоговый учёт · данные БД')[-1],r'Выведено \(с комиссиями\)\s+0\.000000118')
        self.assertEqual(self.sent_count(),1)

    def test_screen_shows_checks_plan_receipt_and_pool_without_false_completion(self):
        self.plan()
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 2, error)
        for text in ('Туннель', 'Валидатор: OK', 'Архив: OK', 'Баланс: OK',
                     'Адрес получателя', A, '0.000000100', '0.000000001',
                     'Принято нодой', 'nonce=4', 'hash4', 'В пуле', '0/1', '20'):
            self.assertIn(text, out)
        self.assertLess(out.index('Баланс: OK'), out.index('Принято нодой'))
        self.assertNotIn('Сверка завершена', out)
        self.assertEqual(self.sent_count(), 1)

    def test_resume_screen_shows_inclusion_depth_without_resending(self):
        self.plan()
        self.run_entry('send_payout.py', '--epoch', '8')
        self.fake.include(depth=3)
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 2, error)
        self.assertIn(A, out)
        self.assertIn('Applied · подтверждения 3/20', out)
        self.assertNotIn('Принято нодой', out)
        self.assertEqual(self.sent_count(), 1)

    def test_bad_health_never_reports_checks_as_successful(self):
        self.plan()
        self.fake.healthy = False
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 1)
        self.assertIn('Проверка валидатора и архива', out)
        self.assertNotIn('Валидатор: OK', out)
        self.assertNotIn('Архив: OK', out)
        self.assertNotIn('unlock', self.fake.calls)

    def test_lost_response_does_not_print_accepted_receipt(self):
        self.plan()
        self.fake.fail = 'submit'
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 1)
        self.assertIn('Отправка 1/1', out)
        self.assertNotIn('Принято нодой', out)
        self.assertEqual(self.workflow.store().list_intents()[0]['status'], 'unknown')

    def test_completed_screen_has_final_proof_before_accounting_message(self):
        self.plan()
        self.include = True
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertIn('Applied · подтверждения 20/20', out)
        self.assertLess(out.index('Applied · подтверждения 20/20'), out.index('Сверка завершена'))

    def test_broken_progress_renderer_cannot_change_submission_or_settlement(self):
        from payout_display import PayoutDisplay
        digest=self.plan()
        self.include=True
        with patch.object(PayoutDisplay,'__call__',side_effect=OSError('closed display')):
            code,out,error=self.run_entry('send_payout.py','--epoch','8')
        self.assertEqual(code,0,error)
        self.assertEqual(self.sent_count(),1)
        self.assertEqual(self.workflow.store().settlement(digest),{})

    def snapshots(self):
        return list((self.root / '.payout-state/backups').glob('*/journal.sqlite3'))

    def test_send_entrypoint_backs_up_before_rpc_and_settles_after_proofs(self):
        digest = self.plan()
        self.include = True
        original = self.fake.submit

        def submit(payment, nonce):
            snapshots = self.snapshots()
            self.assertTrue(snapshots, 'no pre-payment backup')
            self.assertTrue(any(StateStore(p.parent).list_intents() == [] for p in snapshots))
            return original(payment, nonce)

        self.fake.submit = submit
        code, _, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.workflow.store().settlement(digest), {})
        self.assertTrue(any(StateStore(p.parent).settlement(digest) == {} for p in self.snapshots()))

    def test_dry_run_does_not_backup_or_change_journal(self):
        self.plan()
        before = (self.root / '.payout-state/journal.sqlite3').read_bytes()
        code, out, _ = self.run_entry('send_payout.py', '--epoch', '8', '--dry-run')
        self.assertEqual(code, 0)
        self.assertTrue(__import__('json').loads(out)['dry_run'])
        self.assertEqual(before, (self.root / '.payout-state/journal.sqlite3').read_bytes())
        self.assertEqual(self.snapshots(), [])
        self.assertEqual(self.sent_count(), 0)

    def test_backup_failure_prevents_unlock(self):
        self.plan()
        with patch.object(StateStore, 'backup', side_effect=OSError('disk full')):
            code, _, _ = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertNotEqual(code, 0)
        self.assertNotIn('unlock', self.fake.calls)
        self.assertEqual(self.workflow.store().list_intents(), [])

    def test_pending_is_not_reported_complete_and_same_command_finishes_without_resend(self):
        digest = self.plan()
        code, out, err = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 2)
        self.assertIn('mina_pay --epoch 8', out + err)
        self.assertIsNone(self.workflow.store().settlement(digest))
        self.include = True
        code, _, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(self.workflow.store().settlement(digest), {})

    def test_lost_send_response_is_resolved_without_repeating_transfer(self):
        digest = self.plan()
        self.fake.fail = 'submit'
        code, _, _ = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.workflow.store().list_intents()[0]['status'], 'unknown')
        self.fake.fail = None
        self.include = True
        code, _, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(self.workflow.store().settlement(digest), {})

    def test_empty_round_is_settled_without_wallet_unlock(self):
        digest = self.plan(empty=True)
        code, _, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.workflow.store().settlement(digest), {A: 7})
        self.assertNotIn('unlock', self.fake.calls)

    def test_withdrawal_reuses_pending_operation_without_manual_uuid(self):
        code, _, _ = self.run_entry('withdraw.py', '--amount', '0.000000010')
        self.assertEqual(code, 2)
        digest = self.workflow.store().list_plans()[0][0]
        self.include = True
        code, _, error = self.run_entry('withdraw.py', '--amount', '0.000000010')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(len(self.workflow.store().list_plans()), 1)
        self.assertEqual(self.workflow.store().settlement(digest), {})

    def test_pending_withdrawal_with_different_amount_does_not_start_new_operation(self):
        self.run_entry('withdraw.py', '--amount', '0.000000010')
        self.include = True
        code, _, _ = self.run_entry('withdraw.py', '--amount', '0.000000011')
        self.assertNotEqual(code, 0)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(len(self.workflow.store().list_plans()), 1)

    def test_repeated_settled_withdrawal_requires_explicit_new_operation(self):
        self.include = True
        self.assertEqual(self.run_entry('withdraw.py', '--amount', '0.000000010')[0], 0)
        self.assertEqual(self.run_entry('withdraw.py', '--amount', '0.000000010')[0], 0)
        self.assertEqual(self.sent_count(), 1)
        code, _, error = self.run_entry('withdraw.py', '--amount', '0.000000010', '--new-operation')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.sent_count(), 2)

    def test_shallow_or_missing_proof_does_not_settle(self):
        digest = self.plan()
        self.run_entry('send_payout.py', '--epoch', '8')
        self.fake.include(depth=19)
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 2)
        self.fake.chain = []
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 2)
        self.assertIsNone(self.workflow.store().settlement(digest))
        self.assertEqual(self.sent_count(), 1)

    def test_ctrl_c_during_wait_is_resumable_without_traceback(self):
        digest = self.plan()
        with patch('time.sleep', side_effect=KeyboardInterrupt):
            try:
                code, out, error = self.run_entry('send_payout.py', '--epoch', '8', wait='30')
            except KeyboardInterrupt:
                self.fail('interactive interruption escapes instead of returning a resumable status')
        self.assertEqual(code, 130)
        self.assertNotIn('Traceback', out + error)
        self.assertIsNone(self.workflow.store().settlement(digest))
        self.assertEqual(self.sent_count(), 1)
        self.include = True
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 0)
        self.assertEqual(self.sent_count(), 1)

    def test_post_settlement_backup_failure_does_not_enable_resend(self):
        digest = self.plan()
        self.include = True
        original = StateStore.backup
        calls = []

        def backup(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise OSError('disk full after successful settlement')
            return original(store, *args, **kwargs)

        with patch.object(StateStore, 'backup', new=backup):
            self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 1)
        self.assertEqual(self.workflow.store().settlement(digest), {})
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 0)
        self.assertEqual(self.sent_count(), 1)

    def resume_args(self, error):
        commands = [line.split(': ', 1)[1] for line in error.splitlines()
                    if line.startswith('Для продолжения: ')]
        self.assertEqual(len(commands), 1, 'must provide an operation-bound continuation')
        tokens = shlex.split(commands[0])
        self.assertEqual(tokens[0], 'mina_withdraw')
        self.assertIn('--operation-id', tokens)
        self.assertNotIn('--new-operation', tokens)
        self.assertIn('--project-root', tokens)
        self.assertIn('--config', tokens)
        self.assertFalse(Path(tokens[tokens.index('--project-root') + 1]).is_absolute())
        self.assertFalse(Path(tokens[tokens.index('--config') + 1]).is_absolute())
        return tokens[1:]

    def test_new_withdraw_interrupted_after_settlement_resumes_bound_id(self):
        self.include = True
        original = StateStore.backup
        calls = []

        def backup(store, *args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise KeyboardInterrupt
            return original(store, *args, **kwargs)

        with patch.object(StateStore, 'backup', new=backup):
            code, _, error = self.run_entry('withdraw.py', '--amount', '0.000000010', '--new-operation')
        self.assertEqual(code, 130)
        resume = self.resume_args(error)
        code, _, error = self.run_entry('withdraw.py', *resume, include_context=False)
        self.assertEqual(code, 0, error)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(len(self.workflow.store().list_plans()), 1)

    def test_new_withdraw_lost_response_emits_bound_continuation(self):
        self.fake.fail = 'submit'
        code, _, error = self.run_entry('withdraw.py', '--amount', '0.000000010', '--new-operation')
        self.assertEqual(code, 1)
        resume = self.resume_args(error)
        self.fake.fail = None
        self.include = True
        self.assertEqual(self.run_entry('withdraw.py', *resume, include_context=False)[0], 0)
        self.assertEqual(self.sent_count(), 1)

    def test_unknown_still_absent_cannot_trigger_a_second_rpc(self):
        digest = self.plan()
        self.fake.fail = 'submit'
        self.run_entry('send_payout.py', '--epoch', '8')
        self.fake.fail = None
        self.fake.pool = []
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 2)
        self.assertEqual(self.sent_count(), 1)
        self.assertEqual(self.workflow.store().list_intents()[0]['status'], 'unknown')
        self.assertIsNone(self.workflow.store().settlement(digest))

    def test_failed_nonce_is_accounted_as_carry_not_reported_as_paid(self):
        digest = self.plan()
        self.fake.fail = 'submit'
        self.run_entry('send_payout.py', '--epoch', '8')
        self.fake.fail = None
        self.fake.include()
        self.fake.chain[0].update(amount_nano=90, tx_hash='different-payment', memo='manual')
        code, out, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.workflow.store().settlement(digest), {A: 100})
        self.assertEqual(self.workflow.store().list_intents()[0]['status'], 'failed')
        self.assertIn('Неоплаченных платежей: 1', out)
        self.assertEqual(self.sent_count(), 1)

    def test_health_failure_during_completion_does_not_commit(self):
        digest = self.plan()
        self.run_entry('send_payout.py', '--epoch', '8')
        self.fake.healthy = False
        self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 1)
        self.assertIsNone(self.workflow.store().settlement(digest))
        self.assertEqual(self.sent_count(), 1)

    def test_failed_withdrawal_is_not_reported_as_delegator_credit(self):
        self.fake.fail = 'submit'
        self.run_entry('withdraw.py', '--amount', '0.000000010')
        self.fake.fail = None
        self.fake.include()
        self.fake.chain[0].update(amount_nano=9, tx_hash='different-payment', memo='manual')
        code, out, error = self.run_entry('withdraw.py', '--amount', '0.000000010')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.workflow.carry(), {})
        self.assertNotIn('переносимых обязательствах', out)
        self.assertIn('--new-operation', out)

    @unittest.skipUnless(importlib.util.find_spec('ledger'), 'optional private ledger is absent')
    def test_same_pay_command_recovers_ledger_ahead_of_journal(self):
        import ledger
        self.root = self.root / 'private'
        self.root.mkdir()
        self.config_path = self.root / 'config.yml'
        self.config_path.write_text(yaml.safe_dump(self.config))
        ledger.save({'balances': {}, 'history': []}, self.root / 'payout_ledger.json')
        self.workflow = Workflow(self.config, self.root, adapter=self.fake)
        self.workflow.initialize(import_legacy=True)
        digest = self.plan()
        self.include = True
        with patch.object(StateStore, 'settle', side_effect=OSError('journal fsync failure')):
            self.assertEqual(self.run_entry('send_payout.py', '--epoch', '8')[0], 1)
        self.assertIsNone(self.workflow.store().settlement(digest))
        code, _, error = self.run_entry('send_payout.py', '--epoch', '8')
        self.assertEqual(code, 0, error)
        self.assertEqual(self.workflow.store().settlement(digest), {})
        self.assertEqual(len(ledger.load(self.root / 'payout_ledger.json')['history']), 1)
        self.assertEqual(self.sent_count(), 1)
