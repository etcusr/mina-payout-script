import importlib.util
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from tests.test_calculation import A,W
from tests.test_workflow import Fake
from payout_workflow import Workflow
from payout_state import StateError, StateStore

@unittest.skipUnless(importlib.util.find_spec('ledger'), 'optional private ledger is absent')
class PrivateWorkflowTests(unittest.TestCase):
    def setUp(self):
        import ledger
        self.ledger=ledger; self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.path=self.root/'payout_ledger.json'
        ledger.save({'balances':{A:'-0.000000025'},'history':[{'epoch':6,'note':'already reconciled'}]},self.path)
        self.before=self.path.read_bytes(); self.fake=Fake()
        receipt={'from':W,'to':A,'amount':'1','fee':'1','nonce':'0','memo':'legacy','hash':'legacy-tx','id':'legacy-id'}
        (self.root/'sended_txs_e6.csv').write_text(repr({'sendPayment':{'payment':receipt}})+'\n')
        self.fake.chain=[{'wallet':W,'recipient':A,'amount_nano':1,'fee_nano':1,'nonce':0,'memo':'legacy','tx_hash':'legacy-tx',
                          'network':'chain','era':'3','status':'applied','on_best_chain':True,'depth':100,'block_hash':'legacy-block'}]
        self.config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain','LEGACY_PROTOCOL_VERSION_ID':3,'DEFAULT_TX_FEE':1}
        self.w=Workflow(self.config,self.root,adapter=self.fake); self.w.initialize(import_legacy=True)
    def test_import_preserves_legacy_bytes_and_era(self):
        self.assertEqual(self.before,self.path.read_bytes()); self.assertEqual(self.w.carry(),{A:-25})
        with self.assertRaises(StateError): self.w.create_plan(6,{'gross_nano':{},'net_nano':{},'carry_before_nano':{A:-25},'carry_after_nano':{A:-25},'policy':{'fee_nano':1}}, {})
    def test_ledger_commit_then_journal_crash_recovers_without_double_delta(self):
        d=self.w.create_plan(8,{'gross_nano':{A:100},'net_nano':{A:75},'carry_before_nano':{A:-25},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
        self.w.send(d,password='x'); self.fake.include()
        with patch.object(StateStore,'settle',side_effect=OSError('simulated fsync failure')):
            with self.assertRaises(OSError): self.w.reconcile(d,commit=True)
        with self.assertRaises(StateError): self.w.carry()
        result=self.w.reconcile(d,commit=True)
        self.assertEqual(result['balances_nano'],{})
        data=self.ledger.load(self.path)
        self.assertEqual(len(data['history']),2); self.assertEqual(data['history'][0],{'epoch':6,'note':'already reconciled'})
        self.assertEqual(data['history'][1]['deltas'][A],self.ledger._amount('0.000000025'))
    def test_missing_imported_ledger_does_not_turn_off_carry(self):
        self.path.unlink()
        with self.assertRaises(StateError): self.w.carry()
    def test_stale_positive_and_negative_ledgers_cannot_reuse_carry(self):
        for initial in (50,-25):
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); path=root/'payout_ledger.json'
                self.ledger.save({'balances':{A:str(initial/1000000000)},'history':[]},path)
                original=path.read_bytes(); fake=Fake(); w=Workflow(self.config,root,adapter=fake); w.initialize(import_legacy=True)
                d=w.create_plan(8,{'gross_nano':{A:100},'net_nano':{A:100+initial},'carry_before_nano':{A:initial},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
                w.send(d,password='x'); fake.include(); w.reconcile(d,commit=True)
                path.write_bytes(original)
                with self.assertRaises(StateError): w.carry()
                with self.assertRaises(StateError): w.reconcile(d,commit=True)
                with self.assertRaises(StateError): w.send(d,password='x')
    def test_missing_or_replaced_digest_history_rejected_even_when_balance_matches(self):
        d=self.w.create_plan(8,{'gross_nano':{A:25},'net_nano':{},'carry_before_nano':{A:-25},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
        self.w.reconcile(d,commit=True); good=self.ledger.load(self.path)
        for change in ('missing','delta','era'):
            import copy
            data=copy.deepcopy(good)
            if change=='missing': data['history'].pop()
            elif change=='delta': data['history'][-1]['deltas'][A]='0.000000026'
            else: data['history'][-1]['era']='4'
            self.ledger.save(data,self.path)
            with self.subTest(change=change),self.assertRaises(StateError): self.w.carry()
    def test_old_ledger_omitting_new_dust_cannot_underreserve_withdrawal(self):
        d=self.w.create_plan(8,{'gross_nano':{A:32},'net_nano':{},'carry_before_nano':{A:-25},'carry_after_nano':{A:7},'policy':{'fee_nano':1}}, {})
        self.w.reconcile(d,commit=True); self.path.write_bytes(self.before)
        with self.assertRaises(StateError): self.w.withdraw(A,995,password='x')
        self.assertNotIn('unlock',self.fake.calls)
    def test_valid_but_changed_initial_ledger_is_not_accepted(self):
        self.ledger.save({'balances':{A:'0.000000050'},'history':[{'epoch':6,'note':'already reconciled'}]},self.path)
        with self.assertRaises(StateError): self.w.carry()
    def test_pending_recovery_rejects_same_digest_with_changed_history(self):
        d=self.w.create_plan(8,{'gross_nano':{A:25},'net_nano':{},'carry_before_nano':{A:-25},'carry_after_nano':{},'policy':{'fee_nano':1}}, {})
        with patch.object(StateStore,'settle',side_effect=OSError('simulated fsync failure')):
            with self.assertRaises(OSError): self.w.reconcile(d,commit=True)
        data=self.ledger.load(self.path); data['history'][-1]['deltas'][A]='0.000000026'; self.ledger.save(data,self.path)
        with self.assertRaises(StateError): self.w.reconcile(d,commit=True)
        self.assertIsNone(self.w.store().settlement(d))
    def test_private_settled_withdrawal_is_idempotent_after_new_carry(self):
        d=self.w.withdraw(A,10,password='x')['plan']; self.fake.include(); self.w.reconcile(d,commit=True)
        p=self.w.create_plan(8,{'gross_nano':{A:32},'net_nano':{},'carry_before_nano':{A:-25},'carry_after_nano':{A:7},'policy':{'fee_nano':1}}, {})
        self.w.reconcile(p,commit=True)
        before=self.path.read_bytes()
        self.assertTrue(self.w.reconcile(d,commit=True)['already_settled'])
        self.assertEqual(self.w.carry(),{A:7}); self.assertEqual(self.path.read_bytes(),before)
