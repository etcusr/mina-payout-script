import contextlib
import io
import unittest
from unittest.mock import patch


class DiagnosticOutputTests(unittest.TestCase):
    def test_plan_without_journal_does_not_claim_payments_are_unsent(self):
        from payout_display import PayoutDisplay
        from tests.test_calculation import A
        output=io.StringIO()
        with contextlib.redirect_stdout(output):
            PayoutDisplay().plan({'epoch':8,'min_confirmations':20,'payments':[{'recipient':A,'amount_nano':100,'fee_nano':1}]})
        self.assertNotIn('не отправлен',output.getvalue())
        self.assertIn('статус не загружен',output.getvalue())

    def test_full_address_is_clickable_without_breaking_table_width(self):
        from payout_display import account_link
        from tests.test_calculation import A
        from tabulate import tabulate
        output=io.StringIO()
        with patch.object(output,'isatty',return_value=True),patch.dict('os.environ',{'TERM':'xterm-256color','NO_HYPERLINK':''}),contextlib.redirect_stdout(output):
            linked=account_link(A)
        self.assertEqual(linked,'\x1b]8;;https://minascan.io/mainnet/account/'+A+'\x1b\\'+A+'\x1b]8;;\x1b\\')
        table=tabulate([[linked,'1.000000000']],headers=['Address','MINA'],disable_numparse=True)
        plain=table.replace('\x1b]8;;https://minascan.io/mainnet/account/'+A+'\x1b\\','').replace('\x1b]8;;\x1b\\','')
        self.assertEqual(plain,tabulate([[A,'1.000000000']],headers=['Address','MINA'],disable_numparse=True))
        with contextlib.redirect_stdout(output): self.assertEqual(account_link(A),A)

    def test_terminal_colors_distinguish_states_and_preserve_full_address(self):
        from payout_display import PayoutDisplay
        from tests.test_calculation import A
        from tests.test_workflow import Fake
        from payout_workflow import command_identity
        plan={'epoch':8,'min_confirmations':20,'payments':[{'recipient':A,'amount_nano':100,'fee_nano':1,'memo':'test'}]}
        fake=Fake(); receipt=fake.submit(plan['payments'][0],4)
        intent={'wallet':receipt['sendPayment']['payment']['from'],'nonce':4,'payment_index':0,'status':'submitted','payment':plan['payments'][0]}
        output=io.StringIO()
        with patch.object(output,'isatty',return_value=True), patch.dict('os.environ',{'NO_COLOR':'','CLICOLOR_FORCE':'','TERM':'xterm-256color'}), contextlib.redirect_stdout(output):
            display=PayoutDisplay(); display.plan(plan)
            display('health_ok',health={'tip_height':100})
            display('proofs',plan=plan,intents=[intent],chain=[],pool=[command_identity(fake.pool[0])])
            display('proofs',plan=plan,intents=[intent],chain=[],pool=[])
        text=output.getvalue()
        for color in ('\x1b[32m','\x1b[33m','\x1b[31m','\x1b[36m'):
            self.assertIn(color,text)
        self.assertIn(A,text)
        self.assertNotIn(A[:9]+'…',text)

    def test_plain_output_and_no_color_have_no_escape_sequences(self):
        from payout_display import paint
        stream=io.StringIO()
        with patch.dict('os.environ',{'NO_COLOR':'','CLICOLOR_FORCE':''}):
            self.assertEqual(paint('OK','green',stream=stream),'OK')
        with patch.object(stream,'isatty',return_value=True),patch.dict('os.environ',{'NO_COLOR':'1','CLICOLOR_FORCE':'1'}):
            self.assertEqual(paint('OK','green',stream=stream),'OK')

    def test_schema_is_still_probed_but_only_logged_in_debug_mode(self):
        import GraphQL
        for debug in ('', '1'):
            output=io.StringIO()
            with patch.object(GraphQL,'_schema_probe',None), \
                    patch.object(GraphQL,'_query_scalar',return_value=1) as query, \
                    patch.dict('os.environ',{'MINA_DEBUG':debug}), \
                    contextlib.redirect_stderr(output):
                result=GraphQL._probe_schema()
            self.assertEqual(query.call_count,4)
            self.assertTrue(result['bic_has_status'])
            self.assertEqual('schema probe' in output.getvalue(),debug=='1')
