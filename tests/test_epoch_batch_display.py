import contextlib
import io
import os
import unittest
from unittest.mock import patch

from payout_app import report
from payout_display import PayoutDisplay


def fixture(count=24, size=30, source='delegators'):
    addresses=['B62q'+str(i).zfill(51) for i in range(count)]
    fees={}
    for start in range(0,count,size):
        group=addresses[start:start+size]
        share,remainder=divmod(6000001,len(group))
        fees.update({a:share+(j<remainder) for j,a in enumerate(group)})
    policy={'transport':'zkapp_batch','batch_size':size,'fee_nano':6000001,
            'network_fee_source':source,'minimum_nano':1}
    calculation={'policy':policy,'net_nano':dict.fromkeys(addresses,1000000000),
                 'gross_nano':{a:1000000000+(fees[a] if source=='delegators' else 0) for a in addresses},
                 'payment_fees_nano':fees,'delegator_fees_nano':fees if source=='delegators' else {},
                 'carry_before_nano':{},'carry_after_nano':{}}
    plan={'epoch':8,'min_confirmations':3,'policy':policy,'calculation':calculation,
          'payments':[{'recipient':a,'amount_nano':1000000000,'fee_nano':fees[a]} for a in addresses]}
    return plan


class EpochBatchDisplayTests(unittest.TestCase):
    def render(self, action):
        output=io.StringIO()
        with contextlib.redirect_stdout(output): action()
        return output.getvalue()

    def test_report_one_network_fee_for_24_recipients_in_both_funding_modes(self):
        for source in ('delegators','validator'):
            plan=fixture(source=source)
            text=self.render(lambda:report({'epoch':8,'preview':True,'calculation':plan['calculation']}))
            self.assertIn('Сетевые комиссии: 0.006000001 MINA',text)
            self.assertIn('Всего с комиссиями: 24.006000001 MINA',text)
            self.assertIn('Пакетов: 1',text)
            self.assertIn('до 30 получателей',text)

    def test_split_plan_shows_packet_membership_and_exact_total(self):
        plan=fixture(31)
        text=self.render(lambda:PayoutDisplay().plan(plan))
        self.assertIn('Пакет',text)
        self.assertIn('Пакетов: 2',text)
        self.assertIn('Сетевые комиссии: 0.012000002 MINA',text)
        self.assertIn('Пакет №2: 1 получателей · комиссия 0.006000001 MINA',text)
        for payment in plan['payments']:
            self.assertIn(payment['recipient'],text)

    def test_batch_plan_preserves_full_clickable_addresses(self):
        plan=fixture(1); output=io.StringIO(); address=plan['payments'][0]['recipient']
        with contextlib.redirect_stdout(output),patch.object(output,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'xterm','NO_HYPERLINK':''}):
            PayoutDisplay().plan(plan)
        self.assertIn('https://minascan.io/mainnet/account/'+address,output.getvalue())
        self.assertIn('\x1b\\'+address+'\x1b]8;;',output.getvalue())

    def test_proof_updates_never_repeat_recipient_table_or_idle_status(self):
        plan=fixture(); display=PayoutDisplay(); output=io.StringIO()
        batch={'index':0,'status':'unknown','nonce':42,'tx_hash':'full-transaction-hash',
               'recipients':plan['payments'],'fee_nano':6000001,
               'proof':{'status':'pending','in_pool':True}}
        with contextlib.redirect_stdout(output):
            display.plan(plan)
            display('batch_proofs',plan=plan,batches=[batch])
            before=output.getvalue()
            display('batch_proofs',plan=plan,batches=[batch])
            self.assertEqual(output.getvalue(),before)
            for depth in (0,1,2,3):
                batch['proof']={'status':'applied' if depth>=3 else 'pending','in_chain_status':'applied','depth':depth,'height':123}
                display('batch_proofs',plan=plan,batches=[batch])
            before=output.getvalue()
            display('batch_proofs',plan=plan,batches=[batch])
            self.assertEqual(output.getvalue(),before)
        text=output.getvalue()
        for payment in plan['payments']: self.assertEqual(text.count(payment['recipient']),1)
        for expected in ('nonce=42','full-transaction-hash','0/3','1/3','2/3','3/3','Подтверждено пакетов 1/1'):
            self.assertIn(expected,text)

    def test_proof_loss_and_depth_regression_are_visible(self):
        plan=fixture(); display=PayoutDisplay()
        batch={'index':0,'status':'confirmed','nonce':42,'tx_hash':'hash',
               'proof':{'status':'applied','depth':3,'height':123}}
        self.render(lambda:display.batch_proofs(plan,[batch]))
        batch['proof']={'status':'pending','in_chain_status':'applied','depth':1,'height':123}
        self.assertIn('Глубина снизилась',self.render(lambda:display.batch_proofs(plan,[batch])))
        batch['proof']={'status':'unknown'}
        text=self.render(lambda:display.batch_proofs(plan,[batch]))
        self.assertIn('исход неизвестен',text)
        self.assertIn('повторная отправка запрещена',text)
        self.assertIn('Подтверждено пакетов 0/1',text)

    def test_terminal_progress_reuses_one_line(self):
        plan=fixture(); display=PayoutDisplay(); output=io.StringIO()
        batch={'index':0,'status':'unknown','nonce':42,'tx_hash':'hash',
               'proof':{'status':'pending','in_chain_status':'applied','depth':1,'height':123}}
        with contextlib.redirect_stdout(output),patch.object(output,'isatty',return_value=True),patch.dict(os.environ,{'TERM':'xterm','NO_COLOR':''}),patch('shutil.get_terminal_size',return_value=os.terminal_size((180,50))):
            display.batch_proofs(plan,[batch])
            before=len(output.getvalue()); batch['proof']['depth']=2
            display.batch_proofs(plan,[batch])
            self.assertIn('\r\x1b[2K',output.getvalue()[before:])

    def test_old_ordinary_plan_retains_per_payment_fee_reporting(self):
        plan=fixture(2); del plan['policy']['transport']; del plan['calculation']['payment_fees_nano']
        text=self.render(lambda:report({'epoch':8,'preview':True,'calculation':plan['calculation']}))
        self.assertIn('Сетевые комиссии: 0.012000002 MINA',text)
        self.assertNotIn('Пакетов:',text)
        text=self.render(lambda:PayoutDisplay().plan(plan))
        self.assertNotIn('Пакет',text)
