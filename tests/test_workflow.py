import tempfile
from pathlib import Path
import unittest
from tests.test_calculation import A,B,W
from payout_state import StateError, StateStore, wallet_lock, WalletLocked
try:
    from payout_workflow import Workflow
except ModuleNotFoundError: Workflow=None

class Fake:
    def __init__(self):
        self.calls=[]; self.chain=[]; self.pool=[]; self.nonce=4; self.liquid=1000; self.fail=None; self.healthy=True
    def health(self,epoch=None,wallet=False):
        self.calls.append('health')
        if not self.healthy: raise ValueError('bad archive')
        return {'network':'chain','era':3,'current_epoch':9,'epoch_closed':epoch is None or epoch<9,'tip_hash':'tip','best_chain':[{'state_hash':'block','depth':20}], 'account':{'liquid_nano':self.liquid,'nonce':self.nonce,'inferred_nonce':self.nonce+len(self.pool)},'mempool_pending_nano':sum(int(p['amount'])+int(p['fee']) for p in self.pool),'pooled_commands':self.pool}
    def payments(self,health,epoch=None): return self.chain
    def unlock(self,password):
        self.calls.append('unlock')
        if self.fail=='unlock': raise TimeoutError()
    def lock(self): self.calls.append('lock')
    def submit(self,payment,nonce):
        self.calls.append(('send',nonce))
        p={'from':W,'to':payment['recipient'],'amount':str(payment['amount_nano']),'fee':str(payment['fee_nano']),'memo':payment['memo'],'nonce':str(nonce),'hash':'hash'+str(nonce),'id':'id'+str(nonce)}
        self.pool.append(p)
        if self.fail=='submit': raise TimeoutError()
        if self.fail=='receipt': return {'sendPayment':{'payment':{'id':'broken'}}}
        return {'sendPayment':{'payment':p}}
    def include(self,depth=20):
        self.chain+=[{'wallet':p['from'],'recipient':p['to'],'amount_nano':int(p['amount']),'fee_nano':int(p['fee']),'memo':p['memo'],'nonce':int(p['nonce']),'tx_hash':p['hash'],'network':'chain','era':'3','block_hash':'block','on_best_chain':True,'status':'applied','depth':depth} for p in self.pool]
        self.nonce+=len(self.pool); self.pool=[]

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(Workflow,'transactional workflow is required')
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.fake=Fake(); self.config={'SEND_FROM_ADDRESS':W,'VALIDATOR_ADDRESS':W,'EXPECTED_CHAIN_ID':'chain','LEGACY_PROTOCOL_VERSION_ID':3,'DEFAULT_TX_FEE':1,'TX_CONFIRMATIONS_NUM':20}
        self.w=Workflow(self.config,self.root,adapter=self.fake); self.w.initialize()
    def plan(self,epoch=8,amount=100):
        return self.w.create_plan(epoch,{'gross_nano':{A:amount},'net_nano':{A:amount} if amount else {},'carry_before_nano':{},'carry_after_nano':{},'policy':{'fee_nano':1},'total_reward_nano':amount}, {'ledger_hash':'ledger'})
    def test_intent_is_durable_unknown_before_rpc_and_timeout_never_retries(self):
        d=self.plan(); self.fake.fail='submit'; original=self.fake.submit
        def checked(p,n):
            self.assertEqual(StateStore(self.root/'.payout-state').list_intents()[0]['status'],'unknown')
            return original(p,n)
        self.fake.submit=checked
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertEqual(self.fake.calls[-1],'lock')
        self.fake.fail=None
        self.w.send(d,password='x')
        self.assertEqual(len([c for c in self.fake.calls if isinstance(c,tuple)]),1)
        self.assertEqual(self.w.store().list_intents()[0]['status'],'submitted')
    def test_unlock_lost_response_always_attempts_lock(self):
        d=self.plan(); self.fake.fail='unlock'
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertEqual(self.fake.calls[-2:],['unlock','lock'])
        self.assertFalse(any(isinstance(c,tuple) for c in self.fake.calls))
    def test_prompt_then_fresh_health_before_unlock(self):
        d=self.plan()
        def prompt(plan): self.fake.healthy=False; return 'password'
        with self.assertRaises(ValueError): self.w.send(d,confirm=prompt)
        self.assertNotIn('unlock',self.fake.calls)
    def test_reorg_and_shallow_inclusion_cannot_commit(self):
        d=self.plan(); self.w.send(d,password='x'); self.fake.include(19)
        with self.assertRaises(StateError): self.w.reconcile(d,commit=True)
        self.fake.chain=[]
        with self.assertRaises(StateError): self.w.reconcile(d,commit=True)
        self.assertEqual(self.w.store().settlement(d),None)
    def test_exact_confirmed_settlement_idempotent(self):
        d=self.plan(); self.w.send(d,password='x'); self.fake.include()
        self.assertEqual(self.w.reconcile(d,commit=True)['balances_nano'],{})
        self.assertEqual(self.w.reconcile(d,commit=True)['balances_nano'],{})
        self.assertEqual(self.w.store().list_intents()[0]['status'],'confirmed')
    def test_changed_identity_receipt_fails_closed(self):
        d=self.plan(); self.fake.fail='receipt'
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertEqual(self.w.store().list_intents()[0]['status'],'unknown')
    def test_empty_round_carries_dust_idempotently_without_unlock(self):
        d=self.w.create_plan(8,{'gross_nano':{A:7},'net_nano':{},'carry_before_nano':{},'carry_after_nano':{A:7},'policy':{'fee_nano':1}}, {})
        self.w.send(d,password='x'); self.w.reconcile(d,commit=True); self.w.reconcile(d,commit=True)
        self.assertEqual(self.w.carry(),{A:7}); self.assertNotIn('unlock',self.fake.calls)
    def test_onchain_legacy_tag_blocks_fresh_journal_payment(self):
        d=self.plan(); self.fake.chain=[{'memo':'e8-f1_pool','era':'3','wallet':W}]
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertNotIn('unlock',self.fake.calls)
    def test_missing_journal_and_existing_legacy_artifact_fail_closed(self):
        d=self.plan(); (self.root/'sended_txs_e8.csv').write_text('broken receipt')
        with self.assertRaises(StateError): self.w.send(d,password='x')
        (self.root/'.payout-state/journal.sqlite3').unlink()
        with self.assertRaises(StateError): self.w.send(d,password='x')
    def test_balance_and_lock_protect_send_and_withdraw(self):
        d=self.plan(); self.fake.liquid=100
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertNotIn('unlock',self.fake.calls)
        with wallet_lock(self.root/'.payout-state',W):
            with self.assertRaises(WalletLocked): self.w.send(d,password='x')
            with self.assertRaises(WalletLocked): self.w.withdraw(A,1,password='x')
    def test_dry_run_has_no_wallet_or_journal_mutation(self):
        d=self.plan(); before=(self.root/'.payout-state/journal.sqlite3').read_bytes()
        self.w.send(d,dry_run=True)
        self.assertEqual(before,(self.root/'.payout-state/journal.sqlite3').read_bytes())
        self.assertNotIn('unlock',self.fake.calls)
    def test_changed_policy_plan_cannot_reuse_epoch(self):
        d=self.plan(); self.w.send(d,password='x'); self.fake.include(); self.w.reconcile(d,commit=True)
        with self.assertRaises(StateError): self.plan(amount=101)
    def test_withdraw_reserves_dust_and_rejects_pending_round(self):
        d=self.plan()
        with self.assertRaises(StateError): self.w.withdraw(B,1,password='x')
        self.w.send(d,password='x'); self.fake.include(); self.w.reconcile(d,commit=True)
        self.w.withdraw(B,10,password='x')
        self.assertEqual(len([c for c in self.fake.calls if isinstance(c,tuple)]),2)

    def test_new_csv_export_does_not_block_own_plan(self):
        d=self.plan(); (self.root/'e8_payouts.csv').write_text(f'{A};100;0.0000001\n')
        self.w.send(d,password='x'); self.fake.include(); self.w.reconcile(d,commit=True)
        self.assertEqual(self.w.carry(),{})
    def test_disjoint_local_and_external_pool_reserve_union(self):
        d=self.plan(); self.w.send(d,password='x'); self.fake.include(depth=1)
        # 101 local shallow commitment and 51 unrelated pool commitment.
        self.fake.pool=[{'from':W,'to':B,'amount':'50','fee':'1','nonce':'5','memo':'manual','hash':'external'}]
        store=self.w.store(); plan=store.get_plan(d)
        extra=dict(plan,payments=plan['payments']+[dict(plan['payments'][0],recipient=B,amount_nano=60)])
        self.fake.liquid=180
        with self.assertRaises(StateError): self.w._funds(store,extra,store.list_intents(),self.fake.health())
    def test_failed_nonce_evidence_must_remain_on_chain_at_commit(self):
        d=self.plan(); self.fake.fail='submit'
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.fake.include(); self.fake.chain[0].update(recipient=B,tx_hash='conflict',memo='manual')
        self.w.send(d,password='x')
        self.assertEqual(self.w.store().list_intents()[0]['status'],'failed')
        self.fake.chain=[]
        with self.assertRaises(StateError): self.w.reconcile(d,commit=True)
    def test_modified_imported_legacy_csv_is_not_silently_accepted(self):
        root=self.root/'legacy'; root.mkdir()
        (root/'e6_payouts.csv').write_text(f'{A};100;0.0000001\n')
        w=Workflow(self.config,root,adapter=self.fake); w.initialize(import_legacy=True)
        (root/'e6_payouts.csv').write_text(f'{A};101;0.000000101\n')
        with self.assertRaises(StateError): w.store()
    def test_other_unknown_withdrawal_blocks_unlock(self):
        d=self.plan()
        other={'wallet':W,'network':'chain','era':'3','epoch':9,'kind':'withdrawal','operation_id':'c80adc1c-b456-4f74-954e-039e842ddba7','payments':[{'recipient':B,'amount_nano':1,'fee_nano':1}]}
        store=self.w.store(); digest=store.create_plan(other); intent=store.reserve_intent(digest,0,2); store.mark_unknown(intent['id'],'lost')
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertNotIn('unlock',self.fake.calls)
    def test_rejected_second_plan_does_not_poison_next_epoch(self):
        d=self.plan(); self.w.send(d,password='x'); self.fake.include(); self.w.reconcile(d,commit=True)
        with self.assertRaises(StateError): self.plan(amount=101)
        self.assertEqual(len(self.w.store().list_plans()),1)
        self.fake.health_original=self.fake.health
        self.fake.health=lambda *a,**k: dict(self.fake.health_original(*a,**k),current_epoch=10,epoch_closed=True)
        self.assertIsInstance(self.plan(epoch=9),str)

    def test_reward_reorg_after_plan_freeze_blocks_wallet_unlock(self):
        d=self.w.create_plan(8,{'gross_nano':{A:100},'net_nano':{A:100},'carry_before_nano':{},'carry_after_nano':{},'policy':{'fee_nano':1,'CONFIRMATIONS_NUM':20}}, {'blocks':[{'state_hash':'block'}]})
        oldhealth=self.fake.health
        self.fake.health=lambda *a,**k: dict(oldhealth(*a,**k),best_chain=[])
        with self.assertRaises(StateError): self.w.send(d,password='x')
        self.assertNotIn('unlock',self.fake.calls)
    def test_settled_withdrawal_replay_preserves_later_payout_carry(self):
        sent=self.w.withdraw(A,10,password='x'); self.fake.include(); d=sent['plan']
        self.w.reconcile(d,commit=True)
        p=self.w.create_plan(8,{'gross_nano':{A:7},'net_nano':{},'carry_before_nano':{},'carry_after_nano':{A:7},'policy':{'fee_nano':1}}, {})
        self.w.reconcile(p,commit=True)
        result=self.w.reconcile(d,commit=True)
        self.assertTrue(result['already_settled']); self.assertEqual(self.w.carry(),{A:7})
