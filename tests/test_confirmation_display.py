import contextlib
import io
import os
import unittest
from unittest.mock import patch

from payout_display import PayoutDisplay
from tests.test_calculation import A, B, W


class ConfirmationDisplayTests(unittest.TestCase):
    def setUp(self):
        self.plan={'epoch':8,'min_confirmations':3,'payments':[
            {'recipient':a,'amount_nano':1000000001,'fee_nano':6000001,'memo':'test'} for a in (A,B)]}
        self.intents=[{'wallet':W,'nonce':4+j,'payment_index':j,'status':'submitted',
            'tx_hash':'hash'+str(j),'payment':p} for j,p in enumerate(self.plan['payments'])]
        self.output=io.StringIO(); self.display=PayoutDisplay()

    def chain(self,depth,block='block'):
        return [dict(i['payment'],wallet=W,nonce=i['nonce'],tx_hash=i['tx_hash'],
            status='applied',on_best_chain=True,depth=depth,block_hash=block,height=123) for i in self.intents]

    def show(self,depth=None,**changes):
        data=dict(plan=self.plan,intents=self.intents,chain=[] if depth is None else self.chain(depth),pool=[])
        data.update(changes)
        with contextlib.redirect_stdout(self.output): self.display.proofs(**data)

    def test_depth_growth_does_not_repeat_addresses_and_idle_polls_are_quiet(self):
        for depth in (0,1,2): self.show(depth)
        text=self.output.getvalue()
        self.assertEqual(text.count(A),1)
        for value in ('0/3','1/3','2/3'): self.assertIn(value,text)
        for _ in range(5): self.show(2)
        self.assertEqual(self.output.getvalue(),text)

    def test_table_separates_columns_and_completion_is_printed_only_once(self):
        self.show(0); self.show(3)
        text=self.output.getvalue()
        self.assertEqual(text.count(A),2)
        self.assertIn('│',text); self.assertIn('─',text)
        self.assertIn('1.000000001',text); self.assertIn('Nonce',text)
        self.show(4)
        self.assertEqual(self.output.getvalue().count(A),2)

    def test_status_changes_group_payment_numbers_instead_of_reprinting_rows(self):
        pool=[dict(i['payment'],wallet=W,nonce=i['nonce']) for i in self.intents]
        self.show(pool=pool)
        before=len(self.output.getvalue()); self.show(0)
        update=self.output.getvalue()[before:]
        self.assertNotIn(A,update); self.assertNotIn(B,update)
        self.assertIn('№1–2',update); self.assertIn('Applied',update)

    def test_lost_proof_and_regressed_depth_are_not_hidden(self):
        self.show(2)
        before=len(self.output.getvalue()); self.show(1)
        self.assertIn('снизилась',self.output.getvalue()[before:])
        before=len(self.output.getvalue()); self.show()
        self.assertIn('не найден',self.output.getvalue()[before:].lower())

    def test_tty_updates_one_line_and_finishes_before_other_output(self):
        with patch.object(self.output,'isatty',return_value=True), \
             patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':''}), \
             patch('shutil.get_terminal_size',return_value=os.terminal_size((180,50))):
            self.show(0)
            before=len(self.output.getvalue()); self.show(1)
            update=self.output.getvalue()[before:]
            self.assertIn('\r\x1b[2K',update); self.assertNotIn(A,update)
            with contextlib.redirect_stdout(self.output): self.display.line('Connection unavailable')
        self.assertTrue(self.output.getvalue().endswith('\nConnection unavailable\n'))

    def test_narrow_terminal_does_not_overwrite_wrapped_lines(self):
        with patch.object(self.output,'isatty',return_value=True), \
             patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':'1','NO_HYPERLINK':'1'}), \
             patch('shutil.get_terminal_size',return_value=os.terminal_size((40,20))):
            self.show(0); self.show(1)
        self.assertNotIn('\x1b[2K',self.output.getvalue())
        self.assertIn(A,self.output.getvalue())

    def test_plain_log_gets_one_heartbeat_per_minute_not_every_poll(self):
        with patch('time.monotonic',return_value=100): self.show(0)
        before=self.output.getvalue()
        with patch('time.monotonic',return_value=115): self.show(0)
        self.assertEqual(self.output.getvalue(),before)
        with patch('time.monotonic',return_value=161): self.show(0)
        self.assertEqual(self.output.getvalue().count(A),1)
        self.assertGreater(len(self.output.getvalue()),len(before))

    def test_mixed_inclusion_depths_show_range_and_separate_confirmed_count(self):
        chain=self.chain(1); chain[1]['depth']=4
        self.show(chain=chain)
        text=self.output.getvalue()
        self.assertIn('Подтверждено 1/2',text)
        self.assertIn('1/3',text); self.assertIn('4/3',text)
        chain[1]['depth']=2
        self.show(chain=chain)
        self.assertIn('подтверждения 1–2/3',self.output.getvalue())

    def test_new_plan_resets_render_history(self):
        self.show(0)
        self.plan=dict(self.plan,epoch=9)
        self.show(0)
        self.assertEqual(self.output.getvalue().count(A),2)

    def test_dumb_terminal_has_no_cursor_control(self):
        with patch.object(self.output,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'dumb','CLICOLOR_FORCE':'','NO_COLOR':''}):
            self.show(0); self.show(1)
        self.assertNotIn('\x1b',self.output.getvalue())

    def test_resizing_finishes_wrapped_progress_before_suppressing_idle_output(self):
        with patch.object(self.output,'isatty',return_value=True), \
             patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':''}):
            with patch('shutil.get_terminal_size',return_value=os.terminal_size((180,50))): self.show(0)
            before=len(self.output.getvalue())
            with patch('shutil.get_terminal_size',return_value=os.terminal_size((40,50))): self.show(0)
            self.assertTrue(self.output.getvalue()[before:].startswith('\n'))
            before=len(self.output.getvalue())
            with patch('shutil.get_terminal_size',return_value=os.terminal_size((180,50))): self.show(1)
            self.assertNotIn('\r\x1b[2K',self.output.getvalue()[before:])

    def test_different_transaction_hash_cannot_look_confirmed(self):
        chain=self.chain(3); chain[0]['tx_hash']='other'
        self.show(chain=chain)
        self.assertNotIn('Подтверждено 2/2',self.output.getvalue())
