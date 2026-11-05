import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from tests.test_calculation import A,W
from tests.test_workflow import Fake
from payout_workflow import Workflow
try:
    import payout_app
except ModuleNotFoundError: payout_app=None

class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(payout_app,'end-to-end calculation application required')
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain','LEGACY_PROTOCOL_VERSION_ID':3,'DEFAULT_TX_FEE':1,'MINIMUM_PAYOUT':'0','VALIDATOR_FEE':'0','VALIDATOR_FEE_SP':'0','VALIDATOR_FEE_FOUNDATION':'0','VALIDATOR_FEE_O1LABS':'0'}
        self.fake=Fake(); self.w=Workflow(self.config,self.root,adapter=self.fake); self.w.initialize()
        self.data={'stakes':[{'public_key':A,'balance':'1','timing':None}], 'blocks':[{'reward_nano':100,'snark_nano':0,'fees_nano':0,'slot':1,'winner':A,'state_hash':'block','height':1}], 'base_coinbase_nano':100,'ledger_hash':'ledger','report':{'blocks':[{'mature':True,'lost':False}]},'foundation':[],'labs':[]}
    def test_calculate_export_send_reconcile_end_to_end(self):
        result=payout_app.calculate_epoch(self.w,8,provider=lambda w,e,h:self.data)
        self.assertEqual((self.root/'e8_payouts.csv').read_text(),f'{A};100;0.000000100\n')
        self.w.send(result['plan'],password='x'); self.fake.include(); self.w.reconcile(result['plan'],commit=True)
        self.assertEqual(self.w.carry(),{})
    def test_active_preview_does_not_create_plan_or_legacy_export(self):
        result=payout_app.calculate_epoch(self.w,9,provider=lambda w,e,h:self.data)
        self.assertTrue(result['preview']); self.assertEqual(self.w.store().list_plans(),[])
        self.assertFalse((self.root/'e9_payouts.csv').exists())
    def test_legacy_status_does_not_recalculate_or_rewrite(self):
        receipt={'sendPayment':{'payment':{'id':'old','from':W,'to':A,'amount':'100','fee':'1','nonce':'4','memo':'e6-f1_pool'}}}
        path=self.root/'sended_txs_e6.csv'; path.write_text(repr(receipt)+'\n'); before=path.read_bytes()
        result=payout_app.calculate_epoch(self.w,6,provider=lambda *a: self.fail('legacy calculation must not execute'))
        self.assertEqual(result['legacy']['sent_nano'],100); self.assertEqual(before,path.read_bytes())
        self.assertEqual(self.w.store().list_plans(),[])
    def test_closed_epoch_immature_reward_blocks_plan(self):
        self.data['report']['blocks'][0]['mature']=False
        with self.assertRaises(ValueError): payout_app.calculate_epoch(self.w,8,provider=lambda w,e,h:self.data)
        self.assertFalse((self.root/'e8_payouts.csv').exists())
    def test_cli_wrappers_help_from_unrelated_cwd_without_config(self):
        project=Path(__file__).resolve().parents[1]
        for name in ('calc_rewards.py','send_payout.py','withdraw.py','reconcile.py','payout_cli.py'):
            if not (project/name).exists(): continue
            result=subprocess.run([sys.executable,str(project/name),'--help'],cwd=self.root,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,(name,result.stderr))
        self.assertEqual(list(self.root.glob('*.csv')),[])
    def test_cli_explicit_init_backup_restore_offline(self):
        import payout_cli
        other=self.root/'other'; other.mkdir()
        config=other/'config.yml'
        import yaml
        config.write_text(yaml.safe_dump(dict(self.config,ARCHIVE_DB_URL='postgresql://reader@localhost/db')))
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(payout_cli.main(['init','--project-root',str(other),'--config',str(config)]),0)
            self.assertEqual(payout_cli.main(['backup','--project-root',str(other),'--config',str(config),'--destination',str(self.root/'backup')]),0)
            self.assertEqual(payout_cli.main(['restore','--backup',str(self.root/'backup'),'--destination',str(self.root/'restored')]),0)
        self.assertTrue((self.root/'restored/journal.sqlite3').exists())
        self.assertTrue((other/'.payout-state/journal.sqlite3').exists())
    def test_unsafe_reconcile_flags_cannot_commit_csv(self):
        import payout_cli
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(payout_cli.main(['reconcile','--epoch','6','--commit','--from-csv']),1)
            self.assertEqual(payout_cli.main(['reconcile','--epoch','6','--force']),1)
    def test_new_era_same_epoch_does_not_reinterpret_old_paid_epoch(self):
        receipt={'sendPayment':{'payment':{'id':'old','from':W,'to':A,'amount':'100','fee':'1','nonce':'4','memo':'e6-f1_pool'}}}
        (self.root/'sended_txs_e6.csv').write_text(repr(receipt)+'\n')
        oldhealth=self.fake.health
        def health(*args,**kwargs): return dict(oldhealth(*args,**kwargs),era=4)
        self.fake.health=health
        result=payout_app.calculate_epoch(self.w,6,preview=True,provider=lambda w,e,h:self.data)
        self.assertNotIn('legacy',result); self.assertEqual(result['era'],'4')
    def test_cli_send_uses_fake_adapter_and_never_reads_cwd_config(self):
        import payout_cli
        import yaml
        result=payout_app.calculate_epoch(self.w,8,provider=lambda w,e,h:self.data)
        config=self.root/'config.yml'; config.write_text(yaml.safe_dump(dict(self.config,ARCHIVE_DB_URL='postgresql://reader@localhost/db')))
        output=io.StringIO()
        with patch('payout_workflow.LiveAdapter',return_value=self.fake),patch.object(payout_cli.tunnel,'ensure',return_value={}),patch('getpass.getpass',return_value='fake-password'),contextlib.redirect_stdout(output):
            code=payout_cli.main(['send','--plan',result['plan'],'--config',str(config),'--project-root',str(self.root),'--yes','--wait-seconds','0'])
        self.assertEqual(code,0); self.assertEqual(self.w.store().list_intents()[0]['nonce'],4)
        self.assertIn(A,output.getvalue())
        self.assertIn('0.000000100',output.getvalue())
        self.assertEqual(self.fake.calls[-1],'health')
    def test_cli_legacy_commit_is_rejected_without_mutation(self):
        import payout_cli
        import yaml
        receipt={'sendPayment':{'payment':{'id':'old','from':W,'to':A,'amount':'100','fee':'1','nonce':'4','memo':'e6-f1_pool'}}}
        path=self.root/'sended_txs_e6.csv'; path.write_text(repr(receipt)+'\n'); before=path.read_bytes()
        config=self.root/'config.yml'; config.write_text(yaml.safe_dump(dict(self.config,ARCHIVE_DB_URL='postgresql://reader@localhost/db')))
        with patch('payout_workflow.LiveAdapter',return_value=self.fake),contextlib.redirect_stderr(io.StringIO()):
            code=payout_cli.main(['reconcile','--epoch','6','--commit','--config',str(config),'--project-root',str(self.root)])
        self.assertEqual(code,1); self.assertEqual(before,path.read_bytes()); self.assertNotIn('unlock',self.fake.calls)
    def test_balance_report_keeps_exact_liquid_and_obligation_display(self):
        self.fake.liquid=9007199254740993
        result=payout_app.calculate_epoch(self.w,9,provider=lambda w,e,h:self.data)
        output=io.StringIO()
        with contextlib.redirect_stdout(output): payout_app.report(result)
        self.assertEqual(result['wallet']['liquid_nano'],9007199254740993)
        self.assertIn('9007199.254740993',output.getvalue())
        self.assertIn('В ожидании отправки из кошелька',output.getvalue())

    def test_repeat_balance_preserves_plan_and_restores_live_context(self):
        first=payout_app.calculate_epoch(self.w,8,provider=lambda *a:self.data)
        self.w.send(first['plan'],password='x')
        before=(self.root/'.payout-state/journal.sqlite3').read_bytes()
        self.fake.liquid=876
        with patch.object(payout_app,'live_block_report',return_value=self.data['report']):
            repeated=payout_app.calculate_epoch(self.w,8,provider=lambda *a:self.fail('must not recalculate'))
        self.assertEqual(repeated['plan'],first['plan'])
        self.assertEqual(repeated['calculation'],first['calculation'])
        self.assertEqual(repeated['wallet']['liquid_nano'],876)
        self.assertEqual(repeated['block_report'],self.data['report'])
        self.assertEqual(repeated['payment_status']['counts'],{'submitted':1})
        self.assertFalse(repeated['payment_status']['settled'])
        self.assertEqual(before,(self.root/'.payout-state/journal.sqlite3').read_bytes())
        output=io.StringIO()
        with contextlib.redirect_stdout(output): payout_app.report(repeated)
        text=output.getvalue()
        for value in ('Эпоха 8','Сохранённый расчёт','Начислено','Долг до','К выплате','Остаток после','Сетевые комиссии','отправлен','Блоки валидатора','0.000000876',A):
            self.assertIn(value,text)

    def test_balance_table_explains_carry_and_uses_planned_fees(self):
        result={'epoch':8,'plan':'digest','preview':False,'calculation':{
            'gross_nano':{A:15},'carry_before_nano':{A:5},'net_nano':{},
            'carry_after_nano':{A:20},'policy':{'minimum_nano':30,'fee_nano':9}}}
        output=io.StringIO()
        with contextlib.redirect_stdout(output): payout_app.report(result)
        text=output.getvalue()
        for value in ('0.000000015','0.000000005','0.000000020','ниже минимума','0.000000030','Сетевые комиссии: 0.000000000'):
            self.assertIn(value,text)
    def test_status_and_plan_json_stdout_are_complete_json_with_stderr_diagnostics(self):
        import GraphQL
        import payout_cli
        import yaml
        config=self.root/'config.yml'; config.write_text(yaml.safe_dump(dict(self.config,ARCHIVE_DB_URL='postgresql://reader@localhost/db')))
        d=payout_app.calculate_epoch(self.w,8,provider=lambda w,e,h:self.data)['plan']
        oldhealth=self.fake.health
        def health(*args,**kwargs):
            GraphQL._log('fixture archive diagnostic')
            return oldhealth(*args,**kwargs)
        self.fake.health=health
        original=payout_app.calculate_epoch
        def calculation(w,e,preview=False): return original(w,e,preview=preview,provider=lambda *a:self.data)
        for args in (['status','--plan',d,'--online'],['plan','--epoch','9','--json']):
            out=io.StringIO(); err=io.StringIO()
            with patch('payout_workflow.LiveAdapter',return_value=self.fake),patch.object(payout_cli.tunnel,'ensure',return_value={}),patch.object(payout_app,'calculate_epoch',side_effect=calculation),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
                code=payout_cli.main(args+['--config',str(config),'--project-root',str(self.root)])
            self.assertEqual(code,0); self.assertIsInstance(json.loads(out.getvalue()),dict)
            self.assertIn('fixture archive diagnostic',err.getvalue())
    def test_plan_json_with_explicit_vrf_keeps_one_json_document(self):
        import payout_cli
        import yaml
        config=self.root/'config.yml'; config.write_text(yaml.safe_dump(dict(self.config,ARCHIVE_DB_URL='postgresql://reader@localhost/db')))
        original=payout_app.calculate_epoch
        def calculation(w,e,preview=False): return original(w,e,preview=preview,provider=lambda *a:self.data)
        def vrf(*args,**kwargs): print('VRF scan diagnostic'); return {'slots':[]}
        out=io.StringIO(); err=io.StringIO()
        with patch('payout_workflow.LiveAdapter',return_value=self.fake),patch.object(payout_cli.tunnel,'ensure',return_value={}),patch.object(payout_app,'calculate_epoch',side_effect=calculation),patch.object(payout_app,'vrf_report',side_effect=vrf),contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            code=payout_cli.main(['plan','--epoch','9','--json','--vrf','--config',str(config),'--project-root',str(self.root)])
        self.assertEqual(code,0); self.assertEqual(json.loads(out.getvalue())['vrf'],{'slots':[]})
        self.assertIn('VRF scan diagnostic',err.getvalue())
