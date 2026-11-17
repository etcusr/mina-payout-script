"""Epoch transport using the tested batch journal under the ordinary wallet lock.

This module never borrows carry or treats delegator payments as owner withdrawals.
The caller holds the shared wallet lock; ordinary settlement stays in Workflow.
"""
import json
from batch_accounting import BatchStore,FILENAME
from batch_mainnet import BatchWorkflow,MainnetAdapter,signer_packet
from payout_batch_state import batch_plan,linked_digest
from payout_state import StateError


class EpochBatchAdapter(MainnetAdapter):
    def __init__(self,workflow,plan,digest):
        self.workflow=workflow;self.plan=plan;self.digest=digest
        self.config=workflow.config;self.root=workflow.root;self.state_dir=workflow.state_dir
    def health(self):
        w=self.workflow;w._emit('health_start')
        if hasattr(w.adapter,'batch_health'):
            health=w.adapter.batch_health(epoch=self.plan['epoch'])
        else:
            health=super().health(epoch=self.plan['epoch'])
        w._emit('health_ok',health=health)
        return health
    def budget(self,health,batch_digest=None):
        w=self.workflow;store=w.store()
        reserve=sum(max(0,n) for n in self.plan['calculation']['carry_after_nano'].values())
        funds=w._available_funds(store,health,reserve)
        remaining=sum(p['amount_nano']+p['fee_nano'] for p in self.plan['payments'])
        own=0
        if batch_digest:
            bs=BatchStore(w.state_dir,create=False);bp=bs.get_plan(batch_digest)
            remaining=0
            for row in bs.rows(batch_digest):
                if row['status'] not in ('confirmed','failed','cancelled'):
                    remaining+=bp['fee_nano']+sum(p['amount_nano'] for p in bp['batches'][row['index']]['recipients'])
            own=remaining
        available=funds['available_nano']+own
        result={**funds,'available_nano':available,'remaining_nano':remaining,'reserve_nano':reserve}
        if remaining>available:raise StateError('Недостаточно средств для пакетов после резерва делегаторов и других операций.')
        w._emit('funds_ok',funds=result)
        return result
    def approve(self,linked,batch_digest=None):
        w=self.workflow;store=w.store()
        if linked!=batch_plan(self.plan,self.digest):raise StateError('Batch changed from immutable epoch')
        health=self.health();w._check_plan(self.plan,health)
        w._checked_ledger(store);w._unsettled(store,except_digest=self.digest)
        if w.carry()!=self.plan['carry_before_nano']:raise StateError('Carry changed since immutable batch epoch')
        w._verify_previous(store,health,current_digest=self.digest,next_epoch=self.plan['epoch'])
        import owner_income
        income=owner_income.summary(store,w.wallet,w.network)
        if income['baseline_nonce'] is not None:
            owner_income.check_external_spends(store,w.wallet,w.network,health,income)
        if health['account']['nonce']!=health['account']['inferred_nonce'] or health.get('pooled_commands') or health.get('pooled_zkapps'):
            raise StateError('Ожидающие транзакции блокируют следующий пакет.')
        marker=store.batch_link(self.digest)
        if marker:
            rows=BatchStore(w.state_dir,create=False).rows(marker['batch_plan'])
            expected_nonce=marker['initial_nonce']+sum(r['status'] in ('confirmed','failed') for r in rows)
            if health['account']['nonce']!=expected_nonce:
                raise StateError('Nonce изменился вне сохранённой пакетной операции; новая отправка остановлена.')
        self.budget(health,batch_digest)
        return health
    def build(self,expected):
        if hasattr(self.workflow.adapter,'build_batch'):return self.workflow.adapter.build_batch(expected)
        return super().build(expected)
    def sign(self,unsigned,expected,password):
        if hasattr(self.workflow.adapter,'sign_batch'):return self.workflow.adapter.sign_batch(unsigned,expected,password)
        return super().sign(unsigned,expected,password)
    def validate(self,signed,expected):
        if hasattr(self.workflow.adapter,'validate_batch'):return self.workflow.adapter.validate_batch(signed,expected)
        return super().validate(signed,expected)
    def submit(self,signed,expected):
        if hasattr(self.workflow.adapter,'submit_batch'):return self.workflow.adapter.submit_batch(signed,expected)
        return super().submit(signed,expected)
    def proof(self,expected,required_depth=3):
        required=max(required_depth,self.workflow.tx_depth(self.plan))
        if hasattr(self.workflow.adapter,'batch_proof'):return self.workflow.adapter.batch_proof(expected,required_depth=required)
        return super().proof(expected,required_depth=required)


def _existing(workflow,plan,digest):
    if not plan['payments']:return None,None
    marker=workflow.store().batch_link(digest)
    if not (workflow.state_dir/FILENAME).exists():
        if marker:raise StateError('Связанный batch-журнал утрачен; восстановите проверенную резервную копию. Повторная выплата запрещена.')
        return None,None
    bs=BatchStore(workflow.state_dir,create=False);bd=linked_digest(plan,digest)
    present=bd in dict(bs.list_plans(workflow.wallet,workflow.network))
    if marker and not present:raise StateError('Связанный batch-план отсутствует; отправка остановлена.')
    return (bs,bd) if present else (None,None)


def needs_send(workflow,plan,digest):
    if not plan['payments']:return False
    bs,bd=_existing(workflow,plan,digest)
    return bs is None or any(r['status'] in ('planned','prepared') for r in bs.rows(bd))


def _emit(workflow,plan,bs,bd,observed):
    if bs is None:return
    proofs={p['index']:p for p in observed};bp=bs.get_plan(bd)
    rows=[{**r,'proof':proofs.get(r['index'],r['proof'] or {}),
           'recipients':bp['batches'][r['index']]['recipients'],'fee_nano':bp['fee_nano']} for r in bs.rows(bd)]
    workflow._emit('batch_proofs',plan=plan,batches=rows,required_depth=max(3,workflow.tx_depth(plan)))


def send(workflow,store,plan,digest,*,password=None,confirm=None,dry_run=False):
    if not plan['payments']:return {'plan':digest,'intents':[],'settled':store.settlement(digest) is not None}
    adapter=EpochBatchAdapter(workflow,plan,digest);bp=batch_plan(plan,digest)
    bs,bd=_existing(workflow,plan,digest)
    if dry_run:
        h=adapter.approve(bp,bd)
        return dict(adapter.budget(h,bd),plan=digest,dry_run=True)
    if bs:
        engine=BatchWorkflow(bs,adapter);observed=engine.reconcile(bd)
        _emit(workflow,plan,bs,bd,observed)
        rows=bs.rows(bd)
        if any(r['status']=='failed' for r in rows) or all(r['status']=='confirmed' for r in rows):
            return {'plan':digest,'intents':store.list_intents(plan_digest=digest),'settled':store.settlement(digest) is not None}
    h=adapter.health();workflow._check_plan(plan,h)
    # A known uncertain packet may still be in the pool. Reconcile only; its
    # unsent suffix may request credentials for later, but cannot submit now.
    unresolved=bs and any(r['status']=='unknown' for r in bs.rows(bd))
    if not unresolved:
        h=adapter.approve(bp,bd)
        if bs is None:
            for b in bp['batches']:
                adapter.build(signer_packet(bp,b['index'],h['account']['nonce']+b['index']))
    if needs_send(workflow,plan,digest):
        if confirm is not None:password=confirm(plan)
        if password is None:raise StateError('Подтверждение и пароль требуются для оставшихся пакетов.')
        workflow._batch_passwords=getattr(workflow,'_batch_passwords',{})
        workflow._batch_passwords[digest]=password
    if bs is None:
        # Ordinary plan is already immutable; creating this exact linked journal
        # is idempotent if the process stops at this boundary.
        fresh=adapter.approve(bp)
        if fresh['account']['nonce']!=h['account']['nonce']:
            raise StateError('Nonce изменился во время подтверждения; пакет не подписан.')
        bs=BatchStore(workflow.state_dir);bd=bs.create_plan(bp)
    if store.batch_link(digest) is None:
        fresh=adapter.approve(bp,bd)
        if fresh['account']['nonce']!=h['account']['nonce']:
            raise StateError('Nonce изменился во время подтверждения; пакет не подписан.')
        store.bind_batch(digest,bd,fresh['account']['nonce'])
    if not unresolved:
        engine=BatchWorkflow(bs,adapter)
        try:engine.send_next(bd,lambda:workflow._batch_passwords[digest])
        except Exception:
            if not any(r['status']=='failed' for r in bs.rows(bd)):raise
    _emit(workflow,plan,bs,bd,[])
    return {'plan':digest,'intents':store.list_intents(plan_digest=digest),'settled':store.settlement(digest) is not None}


def reconcile(workflow,store,plan,digest,*,commit=False):
    from payout_workflow import PendingPayments
    adapter=EpochBatchAdapter(workflow,plan,digest)
    health=adapter.health();workflow._check_plan(plan,health)
    bs,bd=_existing(workflow,plan,digest);observed=[]
    if bs:
        if commit:
            observed=BatchWorkflow(bs,adapter).reconcile(bd)
        else:
            bp=bs.get_plan(bd)
            for row in bs.rows(bd):
                if row['status'] in ('planned','prepared','cancelled'):continue
                valid=adapter.validate(json.dumps(row['signed']),signer_packet(bp,row['index'],row['nonce']))
                if valid['tx_hash']!=row['tx_hash']:raise StateError('Saved batch hash mismatch')
                proof=adapter.proof(bs.expected(bd,row['index']))
                if row['status'] in ('confirmed','failed') and proof.get('status')!=('applied' if row['status']=='confirmed' else 'failed'):
                    raise StateError('Historical batch confirmation disappeared')
                observed.append({'index':row['index'],**proof})
    _emit(workflow,plan,bs,bd,observed)
    intents=store.list_intents(plan_digest=digest)
    if not commit:
        chain=[]
        for i in intents:
            p=next((p for p in observed if p['index']==i['batch_index']),{})
            if p.get('status')=='applied':chain.append({**p,**i['payment'],'wallet':i['wallet'],'nonce':i['nonce'],'tx_hash':i['tx_hash']})
        return {'plan':digest,'commit':False,'intents':intents,'chain_matches':chain}
    terminal=len(intents)==len(plan['payments']) and all(i['status'] in ('confirmed','failed') for i in intents)
    if not terminal:
        if bs:
            rows=bs.rows(bd)
            if not any(r['status'] in ('unknown','failed') for r in rows) and digest in getattr(workflow,'_batch_passwords',{}):
                BatchWorkflow(bs,adapter).send_next(bd,lambda:workflow._batch_passwords[digest])
                _emit(workflow,plan,bs,bd,[])
        raise PendingPayments('Ожидаются подтверждения или оставшиеся пакеты; продолжение через mina_pay.')
    result=workflow._commit_accounting(store,plan,digest,intents,health,'payout')
    getattr(workflow,'_batch_passwords',{}).pop(digest,None)
    return result
