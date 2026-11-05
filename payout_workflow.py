"""Guarded payout operations. All financial writes share one wallet lock.

Network effects are isolated behind LiveAdapter. Unknown submission outcomes
never authorize retry. Legacy artifacts are evidence, never a send queue.
"""
import ast
import hashlib
import json
import re
import uuid
from pathlib import Path
from decimal import Decimal
from money import mina_to_nano, nano_to_mina
from payout_calculation import validate_address, integer
from payout_state import StateStore, StateError, wallet_lock, atomic_write_json

PROJECT=Path(__file__).resolve().parent

class PendingPayments(StateError):
    """Fresh evidence cannot yet settle every payment; no financial commit."""

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()

def decode_memo(value):
    # No archive dependency in pure/fake workflow tests.
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    try:
        number=0
        for c in value: number=number*58+alphabet.index(c)
        raw=number.to_bytes((number.bit_length()+7)//8,'big')
        body=raw[:-4]
        if len(body)==35 and body[:2]==bytes([20,1]) and body[2]<=32 and hashlib.sha256(hashlib.sha256(body).digest()).digest()[:4]==raw[-4:]:
            return body[3:3+body[2]].decode('utf8')
    except (ValueError,TypeError,OverflowError,UnicodeError): pass
    return value

def command_identity(row):
    try:
        def num(key):
            v=row[key]
            if isinstance(v,bool) or not re.fullmatch('[0-9]+',str(v)): raise ValueError()
            return integer(int(v),key)
        return {'wallet':validate_address(row['from']),'recipient':validate_address(row['to']),
                'nonce':num('nonce'),'amount_nano':num('amount'),'fee_nano':num('fee'),'memo':decode_memo(row['memo']),
                'tx_hash':row.get('hash')}
    except (KeyError,TypeError,ValueError): raise StateError('malformed payment evidence; investigate without retry') from None

def same_payment(intent,evidence):
    p=intent['payment']
    return all(evidence.get(k)==v for k,v in {'wallet':intent['wallet'],'nonce':intent['nonce'],'recipient':p['recipient'],
        'amount_nano':p['amount_nano'],'fee_nano':p['fee_nano'],'memo':p.get('memo','')}.items())

def read_legacy_receipts(path):
    rows=[]
    for line in Path(path).read_text().splitlines():
        if not line.strip(): continue
        try: raw=ast.literal_eval(line)['sendPayment']['payment']
        except (ValueError,SyntaxError,KeyError,TypeError): raise StateError('malformed legacy receipt; no records skipped') from None
        row=command_identity(raw)
        row['id']=raw.get('id')
        if not row['id'] and not row['tx_hash']: raise StateError('legacy receipt missing identifier')
        if any(r['nonce']==row['nonce'] for r in rows): raise StateError('duplicate legacy receipt nonce')
        rows.append(row)
    if not rows: raise StateError('empty legacy sent artifact requires investigation')
    return rows

def read_csv(path,*,legacy=False):
    result={}
    for line in Path(path).read_text().splitlines():
        if not line.strip(): continue
        columns=line.split(';')
        try:
            a=validate_address(columns[0]); amount=integer(int(columns[1]))
            display=mina_to_nano(columns[2])
            if abs(display-amount)>(1 if legacy else 0) or a in result: raise ValueError()
        except (ValueError,IndexError): raise StateError('invalid or duplicate CSV payment; nanomina and display must agree') from None
        result[a]=amount
    return result

class LiveAdapter:
    def __init__(self,config):
        self.config=config
        from mina_client import MinaClient
        self.client=MinaClient(graphql_host=config.get('GRAPHQL_HOST','127.0.0.1'),graphql_port=config.get('GRAPHQL_PORT',3085))
    def health(self,epoch=None,wallet=False):
        import preflight
        return preflight.check(self.config,epoch=epoch,require_wallet=wallet)
    def payments(self,health,epoch=None):
        import GraphQL
        if epoch is not None and (health.get('epoch_first_height') is None or
                health.get('checked_epoch') is None or epoch < health['checked_epoch']):
            import preflight
            deeper=preflight.check(self.config,epoch=epoch,require_wallet=False)
            if any(deeper[key]!=health[key] for key in ('tip_hash','network','era')):
                raise StateError('producer moved during historical proof capture; retry read-only check')
            health=deeper
        return GraphQL.getSentPayments(self.config['SEND_FROM_ADDRESS'],epoch=epoch,health=health,config=self.config)
    def unlock(self,password): return self.client.unlock_wallet(self.config['SEND_FROM_ADDRESS'],password)
    def previous_payments(self,health,expected):
        """Locate a height bound across eras, then prove ancestry from the same tip."""
        import GraphQL
        import preflight
        GraphQL.configure(self.config)
        floor=GraphQL._query_scalar('''SELECT MIN(b.height)
            FROM blocks_user_commands buc JOIN user_commands uc ON uc.id=buc.user_command_id
            JOIN blocks b ON b.id=buc.block_id JOIN public_keys pk ON pk.id=uc.source_id
            WHERE pk.value=%(wallet)s AND uc.nonce=ANY(%(nonces)s)
              AND b.protocol_version_id=ANY(%(eras)s) AND buc.status='applied' ''',
            {'wallet':self.config['SEND_FROM_ADDRESS'],
             'nonces':sorted({i['nonce'] for i,_,_,_ in expected}),
             'eras':sorted({int(era) for _,era,_,_ in expected})})
        if floor is None: return []
        if min(b['height'] for b in health['best_chain'])>int(floor):
            deeper=preflight.check(self.config,require_wallet=False,min_height=int(floor))
            if any(deeper[key]!=health[key] for key in ('tip_hash','network','era')):
                raise StateError('Producer moved during previous-payment proof capture; repeat the check.')
            health=deeper
        return GraphQL.getSentPayments(self.config['SEND_FROM_ADDRESS'],health=health,config=self.config)
    def lock(self): return self.client.lock_wallet(self.config['SEND_FROM_ADDRESS'])
    def submit(self,payment,nonce):
        return self.client.send_payment(to_pk=payment['recipient'],from_pk=self.config['SEND_FROM_ADDRESS'],amount=payment['amount_nano'],fee=payment['fee_nano'],memo=payment['memo'],nonce=nonce)

class Workflow:
    def __init__(self,config,root=PROJECT,*,state_dir=None,adapter=None):
        self.config=config; self.root=Path(root).resolve(); self.state_dir=Path(state_dir).resolve() if state_dir else self.root/'.payout-state'
        self.wallet=validate_address(config['SEND_FROM_ADDRESS']); self.network=config['EXPECTED_CHAIN_ID']
        if not self.network: raise StateError('expected network is required')
        self.adapter=adapter or LiveAdapter(config)
        self.observer=None
    def _emit(self,event,**data):
        if self.observer is not None:
            # Rendering cannot turn a durable receipt into an uncertain send.
            try: self.observer(event,**data)
            except Exception: pass
    def health(self,**kwargs):
        self._emit('health_start')
        result=self.adapter.health(**kwargs)
        self._emit('health_ok',health=result)
        return result
    def tx_depth(self,plan):
        return integer(self.config.get('TX_CONFIRMATIONS_NUM',plan.get('min_confirmations',3)), 'TX_CONFIRMATIONS_NUM', 1)

    def _sync_owner_income(self,store,nonce):
        """Recover the settlement/credit boundary before using any income total."""
        import owner_income
        for digest,plan,_ in store.settled_plans(self.wallet,self.network):
            owner_income.record_payout(store,digest,plan,store.list_intents(plan_digest=digest),self.config,nonce)

    def _verify_previous(self,store,health,*,current_digest=None,next_epoch=None):
        """Fresh exact proofs before a new operation; never infer success from age."""
        next_depth=integer(self.config.get('PREVIOUS_PAYOUT_CONFIRMATIONS_NUM',15),'PREVIOUS_PAYOUT_CONFIRMATIONS_NUM',1)
        for record in store.historical_records():
            if record['metadata'].get('kind')!='initialization': continue
            baseline=json.loads(record['payload'])
            if baseline.get('wallet')!=self.wallet: continue
            for epoch in baseline.get('legacy_epochs',[]):
                if not (self.root/f'sended_txs_e{epoch}.csv').is_file():
                    raise StateError(f'Для исторической выплаты эпохи {epoch} нет квитанций; проверка перед новой отправкой невозможна.')
        expected=[]
        for digest,plan in store.list_plans(self.wallet,self.network):
            if digest==current_digest: continue
            intents=store.list_intents(plan_digest=digest)
            if not intents: continue
            if store.settlement(digest) is None:
                raise StateError('Предыдущая операция не сверена; сначала продолжите её исходную команду.')
            required=self.tx_depth(plan)
            if plan.get('kind','payout')=='payout' and next_epoch is not None:
                required=max(required,next_depth)
            for intent in intents:
                if intent['status']=='failed':
                    proof=intent['evidence']
                    payment={k:proof[k] for k in ('recipient','amount_nano','fee_nano','memo')}
                    checked=dict(intent,payment=payment,tx_hash=proof['tx_hash'])
                    required_for_intent=max(required,plan.get('min_confirmations',20))
                elif intent['status']=='confirmed':
                    checked=intent; required_for_intent=required
                else:
                    raise StateError('Исход предыдущего платежа не подтверждён; новая отправка остановлена.')
                expected.append((checked,plan['era'],plan['epoch'],required_for_intent))
        # Imported successful receipts remain obligations to verify too.
        for path in self.root.glob('sended_txs_e*.csv'):
            match=re.fullmatch(r'sended_txs_e(\d+)\.csv',path.name)
            if match is None: continue
            epoch=int(match[1]); era=str(self.config.get('LEGACY_PROTOCOL_VERSION_ID'))
            required=integer(self.config.get('TX_CONFIRMATIONS_NUM',3),'TX_CONFIRMATIONS_NUM',1)
            if next_epoch is not None: required=max(required,next_depth)
            for receipt in read_legacy_receipts(path):
                if receipt['wallet']!=self.wallet: raise StateError('Историческая квитанция относится к другому кошельку.')
                intent={'wallet':self.wallet,'nonce':receipt['nonce'],'tx_hash':receipt['tx_hash'],
                        'payment':{k:receipt[k] for k in ('recipient','amount_nano','fee_nano','memo')}}
                expected.append((intent,era,epoch,required))
        if not expected: return
        self._emit('previous_start',count=len(expected))
        if hasattr(self.adapter,'previous_payments'):
            chain=self.adapter.previous_payments(health,expected)
        else:
            chain=self.adapter.payments(health,epoch=min(row[2] for row in expected))
        depths=[]
        for intent,era,epoch,required in expected:
            matches=[p for p in chain if same_payment(intent,p) and p.get('network')==self.network and
                     str(p.get('era'))==era and (not intent['tx_hash'] or p.get('tx_hash')==intent['tx_hash'])]
            if len(matches)!=1 or matches[0].get('status')!='applied' or matches[0].get('on_best_chain') is not True:
                raise StateError('Предыдущий платёж эпохи '+str(epoch)+' не подтверждён в свежей цепочке; новая отправка остановлена.')
            depth=matches[0].get('depth',-1)
            if depth<required:
                raise StateError(f'Предыдущая выплата эпохи {epoch}: глубина {depth}/{required}. Дождитесь блоков перед новой операцией.')
            depths.append(depth)
        groups={}
        for (_,_,_,required),depth in zip(expected,depths):
            groups.setdefault(required,[]).append(depth)
        self._emit('previous_ok',groups={required:{'count':len(values),'depth':min(values)} for required,values in groups.items()})
    def store(self):
        store=StateStore(self.state_dir)
        if not any(r['metadata'].get('kind')=='initialization' and r['metadata']['network']==self.network and json.loads(r['payload']).get('wallet')==self.wallet for r in store.historical_records()):
            raise StateError('state initialization/import is incomplete or bound to another wallet/network')
        for record in store.historical_records():
            if record['metadata'].get('kind')!='initialization': continue
            for name,digest in json.loads(record['payload']).get('files',{}).items():
                if name=='payout_ledger.json': continue
                path=self.root/name
                if Path(name).name!=name or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=digest:
                    raise StateError('imported legacy artifact is missing or changed; restore original evidence')
        return store
    def _ledger(self):
        path=self.root/'payout_ledger.json'
        if not path.exists():
            # Once imported, deletion must not silently turn private accounting off.
            if (self.state_dir/'journal.sqlite3').exists():
                for r in StateStore(self.state_dir).historical_records():
                    if r['metadata'].get('kind')=='initialization' and 'payout_ledger.json' in json.loads(r['payload']).get('files',{}):
                        raise StateError('imported private ledger is missing')
            return None
        try: import ledger
        except ModuleNotFoundError as e:
            if e.name!='ledger': raise
            raise StateError('private ledger exists but its optional reader is missing') from None
        return ledger.load(path)
    @staticmethod
    def _payout_balances(plan, intents):
        if len(intents)!=len(plan['payments']) or any(i['status'] not in ('confirmed','failed') for i in intents):
            raise StateError('pending/unknown or unsent payments: no financial commit')
        balances=dict(plan['carry_before_nano'])
        for address,amount in plan['gross_nano'].items(): balances[address]=balances.get(address,0)+amount
        for intent in intents:
            if intent['status']=='confirmed':
                address=intent['payment']['recipient']
                balances[address]=balances.get(address,0)-intent['payment']['amount_nano']
        return {a:n for a,n in balances.items() if n}

    def _ledger_entry(self, plan, digest, balances):
        before=plan['carry_before_nano']
        delta={a:balances.get(a,0)-before.get(a,0) for a in set(balances)|set(before)}
        return {'epoch':plan['epoch'],'era':plan['era'],'network':plan['network'],'wallet':self.wallet,
                'plan_digest':digest,'note':'verified applied best-chain settlement',
                'deltas':{a:nano_to_mina(n) for a,n in delta.items()}}

    def _checked_ledger(self, store, *, recover_digest=None):
        """Cross-check both durable stores; recovery is explicit and digest-bound.

        Only reconcile, after fresh chain proofs, supplies recover_digest. Normal
        reads/signing cannot trust a ledger advanced ahead of the journal.
        """
        data=self._ledger()
        if data is None: return None
        records=[json.loads(r['payload']) for r in store.historical_records()
                 if r['metadata'].get('kind')=='initialization' and r['metadata']['network']==self.network
                 and json.loads(r['payload']).get('wallet')==self.wallet]
        if len(records)!=1 or 'payout_ledger.json' not in records[0]['files'] or 'ledger_history' not in records[0]:
            raise StateError('private ledger lacks a complete immutable initialization baseline')
        baseline=records[0]; expected=dict(baseline['balances_nano']); history=list(baseline['ledger_history'])
        for digest,plan,balances in store.settled_plans(self.wallet,self.network):
            if plan.get('kind','payout')!='payout': continue
            if plan['carry_before_nano']!=expected:
                raise StateError('journal payout history does not continue the initialized accounting')
            proved=self._payout_balances(plan,store.list_intents(plan_digest=digest))
            if proved!=balances: raise StateError('journal settlement disagrees with immutable paid accounting')
            history.append(self._ledger_entry(plan,digest,balances)); expected=balances
        actual={a:mina_to_nano(v,allow_negative=True) for a,v in data['balances'].items()}
        if fingerprint(data['history'])==fingerprint(history) and actual==expected: return data
        if recover_digest is not None and store.settlement(recover_digest) is None:
            plan=store.get_plan(recover_digest)
            if plan.get('kind','payout')=='payout' and plan['wallet']==self.wallet and plan['network']==self.network and plan['carry_before_nano']==expected:
                balances=self._payout_balances(plan,store.list_intents(plan_digest=recover_digest))
                recovery_history=history+[self._ledger_entry(plan,recover_digest,balances)]
                if actual==balances and fingerprint(data['history'])==fingerprint(recovery_history): return data
        raise StateError('private ledger diverges from initialized/journal accounting; restore a consistent backup or reconcile the exact pending digest')

    def carry(self):
        store=self.store(); data=self._checked_ledger(store)
        return {a:mina_to_nano(v,allow_negative=True) for a,v in data['balances'].items()} if data is not None else store.carry_balances(self.wallet,self.network)
    def initialize(self,*,import_legacy=False):
        with wallet_lock(self.state_dir,self.wallet):
            if (self.state_dir/'journal.sqlite3').exists(): raise StateError('journal already exists; do not reinitialize financial state')
            artifacts=list(self.root.glob('*.csv'))
            data=self._ledger()
            if (artifacts or data is not None) and not import_legacy: raise StateError('legacy files exist; explicit init --import-legacy required')
            era=self.config.get('LEGACY_PROTOCOL_VERSION_ID')
            if (artifacts or data is not None) and era is None: raise StateError('explicit LEGACY_PROTOCOL_VERSION_ID required for historical import')
            # Strictly parse sent records now. CSV nanos remain authoritative.
            for path in self.root.glob('sended_txs_e*.csv'): read_legacy_receipts(path)
            for path in self.root.glob('e*_payouts.csv'): read_csv(path,legacy=True)
            for path in self.root.glob('e*_owed.csv'): read_csv(path,legacy=True)
            files={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in artifacts}
            if data is not None:
                path=self.root/'payout_ledger.json'; files[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
            baseline={'wallet':self.wallet,'files':files,'legacy_epochs':sorted({h['epoch'] for h in data['history']}) if data else [],'balances_nano':{a:mina_to_nano(v,allow_negative=True) for a,v in data['balances'].items()} if data else {},'ledger_history':data['history'] if data is not None else []}
            path=self.state_dir/'initialization.json'; atomic_write_json(path,baseline)
            store=StateStore(self.state_dir,create=True)
            store.import_history(path,{'kind':'initialization','network':self.network,'era':str(era or 'new'),'epoch':0})
            return {'initialized':True,'legacy_files':len(files),'legacy_epochs':baseline['legacy_epochs']}
    def _legacy_epoch(self,epoch,era):
        if str(self.config.get('LEGACY_PROTOCOL_VERSION_ID'))!=str(era): return False
        data=self._ledger()
        return (self.root/f'sended_txs_e{epoch}.csv').exists() or bool(data and any(h['epoch']==epoch and 'plan_digest' not in h for h in data['history']))
    def _unsettled(self,store,except_digest=None):
        for digest,p in store.list_plans(self.wallet,self.network):
            if digest!=except_digest and p.get('kind','payout')=='payout' and store.settlement(digest) is None:
                raise StateError('another payout plan remains unsettled; reconcile it before using carry or withdrawing')
        data=self._ledger(); history=data['history'] if data else []
        for path in list(self.root.glob('sended_txs_e*.csv'))+list(self.root.glob('e*_payouts.csv')):
            m=re.search(r'(?:sended_txs_e|e)(\d+)',path.name)
            if not m: continue
            epoch=int(m[1])
            known_new=path.name.startswith('e') and any(p['epoch']==epoch and p.get('kind','payout')=='payout' for _,p in store.list_plans(self.wallet,self.network))
            if known_new: continue
            if not any(h['epoch']==epoch and 'plan_digest' not in h for h in history):
                raise StateError('legacy payout artifacts are unreconciled; verify/import historical accounting before proceeding')
    def _check_plan(self,plan,health,kind='payout'):
        if plan.get('kind','payout')!=kind: raise StateError('wrong operation kind')
        if plan['wallet']!=self.wallet or plan['network']!=self.network or str(health['era'])!=plan['era'] or health['network']!=self.network:
            raise StateError('immutable plan network, era or wallet mismatch')
        if kind=='payout' and (not health.get('epoch_closed') or plan['epoch']>=health['current_epoch']): raise StateError('active epochs are preview only')
        ancestry={b['state_hash']:b for b in health['best_chain']}
        for block in plan.get('inputs',{}).get('blocks',[]):
            if block['state_hash'] not in ancestry or ancestry[block['state_hash']]['depth']<plan.get('policy',{}).get('CONFIRMATIONS_NUM',20):
                raise StateError('frozen reward block lost required best-chain ancestry; do not send or settle')
        for p in plan['payments']:
            validate_address(p['recipient']); integer(p['amount_nano'],'amount',1); integer(p['fee_nano'],'fee')
            if len(p.get('memo','').encode())>32: raise StateError('memo exceeds Mina 32-byte limit')
    def create_plan(self,epoch,calculation,inputs,*,health=None):
        with wallet_lock(self.state_dir,self.wallet):
            return self._create_plan(epoch,calculation,inputs,health=health)
    def _create_plan(self,epoch,calculation,inputs,*,health=None):
        store=self.store(); health=health or self.health(epoch=epoch,wallet=False)
        integer(epoch,'epoch'); era=str(health['era'])
        if self._legacy_epoch(epoch,era): raise StateError('legacy epoch already paid/reconciled: read-only status only')
        if any(p['epoch']==epoch and p['era']==era and p.get('kind','payout')=='payout' for _,p in store.list_plans(self.wallet,self.network)):
            raise StateError('epoch already has an immutable plan; use existing plan or a separate read-only preview')
        self._unsettled(store)
        self._verify_previous(store,health,next_epoch=epoch)
        if calculation['carry_before_nano']!=self.carry(): raise StateError('carry changed during calculation')
        seed={'calculation':calculation,'inputs':inputs,'wallet':self.wallet,'network':self.network,'era':era,'epoch':epoch}
        memo=f'p{era}e{epoch}:'+fingerprint(seed)[:16]
        plan=dict(seed,kind='payout',gross_nano=calculation['gross_nano'],carry_before_nano=calculation['carry_before_nano'],
                  policy=calculation['policy'],input_fingerprint=fingerprint(inputs),min_confirmations=integer(self.config.get('TX_CONFIRMATIONS_NUM',3),'TX_CONFIRMATIONS_NUM',1),
                  payments=[{'recipient':a,'amount_nano':n,'fee_nano':calculation['policy']['fee_nano'],'memo':memo} for a,n in sorted(calculation['net_nano'].items())])
        self._check_plan(plan,health)
        digest=store.create_plan(plan); store.bind_plan(digest)
        atomic_write_json(self.state_dir/(digest+'.plan.json'),plan)
        return digest
    def plan_for_epoch(self,epoch):
        plans=[d for d,p in self.store().list_plans(self.wallet,self.network) if p['epoch']==epoch and p.get('kind','payout')=='payout']
        if len(plans)!=1: raise StateError('epoch does not identify exactly one immutable plan; use --plan digest')
        return plans[0]
    def _evidence(self,plan,health):
        chain=self.adapter.payments(health,epoch=plan['epoch'])
        pool=[dict(command_identity(p),network=self.network,era=str(health['era'])) for p in health.get('pooled_commands',[]) if not p.get('isDelegation')]
        return chain,pool
    def _replay_guard(self,plan,chain,pool,intents):
        if plan.get('kind','payout')!='payout': return
        if self._legacy_epoch(plan['epoch'],plan['era']): raise StateError('legacy sent/history artifact blocks sending this epoch')
        prefixes=(f"e{plan['epoch']}-f1_",f"p{plan['era']}e{plan['epoch']}:")
        for row in chain+pool:
            if str(row.get('era'))==plan['era'] and decode_memo(row.get('memo','')).startswith(prefixes) and not any(same_payment(i,row) for i in intents):
                raise StateError('on-chain or pooled payout tag already exists for epoch; verified import required, never resend')
    def _refresh(self,store,plan,digest,chain,pool,*,write=True):
        intents=store.list_intents(plan_digest=digest)
        for i in intents:
            if i['status']=='failed':
                proof=i['evidence']
                if not any(all(r.get(k)==proof.get(k) for k in ('wallet','nonce','recipient','amount_nano','fee_nano','memo','tx_hash','block_hash','network','era')) and r.get('status')=='applied' and r.get('on_best_chain') is True and r.get('depth',-1)>=plan['min_confirmations'] for r in chain):
                    raise StateError('failed nonce proof no longer has confirmed successful ancestry')
                continue
            matches=[p for p in chain if same_payment(i,p)]
            pooled=[p for p in pool if same_payment(i,p)]
            if len(matches)>1 or len(pooled)>1: raise StateError('ambiguous payment evidence')
            row=matches[0] if matches else pooled[0] if pooled else None
            if row:
                if not row.get('tx_hash') or (i['tx_hash'] and i['tx_hash']!=row['tx_hash']): raise StateError('receipt hash conflicts with full payment identity')
                mature=bool(matches and row.get('on_best_chain') is True and row.get('status')=='applied' and row.get('depth',-1)>=self.tx_depth(plan))
                if i['status']=='confirmed' and not mature: raise StateError('previous confirmation lost after reorg; accounting held')
                if write and i['status'] in ('reserved','unknown'): store.mark_submitted(i['id'],row['tx_hash'])
                if write and mature and i['status']!='confirmed': store.mark_confirmed(i['id'],row,required_depth=self.tx_depth(plan))
            elif i['status']=='confirmed': raise StateError('confirmed payment disappeared from authoritative chain')
            elif i['status']!='failed':
                conflicts=[p for p in chain if p.get('wallet')==self.wallet and p.get('nonce')==i['nonce'] and p.get('depth',-1)>=plan['min_confirmations'] and p.get('on_best_chain') is True and p.get('status')=='applied']
                if conflicts and write: store.resolve_failed(i['id'],dict(conflicts[0],kind='nonce_consumed'))
        return store.list_intents(plan_digest=digest)
    def _funds(self,store,plan,intents,health):
        indices={i['payment_index'] for i in intents}
        remaining=sum(p['amount_nano']+p['fee_nano'] for j,p in enumerate(plan['payments']) if j not in indices)
        # Conservative: included-but-shallow local commitments may already be
        # reflected in liquid. Double reservation is safe; missing one is not.
        pending=store.pending_commitments(self.wallet,network=self.network)
        reserve=sum(max(0,n) for n in (plan.get('calculation',{}).get('carry_after_nano',{}) if plan.get('kind','payout')=='payout' else self.carry()).values())
        all_pending=[i for i in store.list_intents(self.wallet,network=self.network) if i['status'] in ('reserved','unknown','submitted')]
        overlap=0
        for row in health.get('pooled_commands',[]):
            if row.get('isDelegation'): continue
            identity=command_identity(row)
            if any(same_payment(i,identity) for i in all_pending): overlap+=identity['amount_nano']+identity['fee_nano']
        commitments=pending+health['mempool_pending_nano']-overlap
        available=health['account']['liquid_nano']-commitments-reserve
        if remaining>available: raise StateError('insufficient uncommitted liquid balance after fees and owed reserves')
        result={'remaining_nano':remaining,'reserved_nano':reserve,'available_nano':available}
        self._emit('funds_ok',funds=result)
        return result
    def send(self,digest,*,password=None,confirm=None,dry_run=False):
        with wallet_lock(self.state_dir,self.wallet):
            return self._send(digest,password=password,confirm=confirm,dry_run=dry_run,kind='payout')
    def _send(self,digest,*,password=None,confirm=None,dry_run=False,kind='payout'):
        store=self.store(); plan=store.get_plan(digest)
        self._checked_ledger(store)
        self._unsettled(store,except_digest=digest)
        health=self.health(epoch=plan['epoch'] if kind=='payout' else None,wallet=True)
        if kind=='withdrawal':
            import owner_income
            owner_income.verify_auto_reservation(store,digest,health)
        self._check_plan(plan,health,kind)
        self._verify_previous(store,health,current_digest=digest,next_epoch=plan['epoch'] if kind=='payout' else None)
        chain,pool=self._evidence(plan,health); intents=store.list_intents(plan_digest=digest)
        self._replay_guard(plan,chain,pool,intents)
        if kind=='payout' and store.settlement(digest) is None and self.carry()!=plan['carry_before_nano']: raise StateError('carry changed since immutable plan; do not send')
        if dry_run: return dict(self._funds(store,plan,intents,health),plan=digest,dry_run=True)
        intents=self._refresh(store,plan,digest,chain,pool)
        if any(i['status'] in ('unknown','reserved') for i in intents): raise StateError('unresolved intent: inspect chain/mempool; blind retry is forbidden')
        if any(i['plan_digest']!=digest and i['status'] in ('unknown','reserved') for i in store.list_intents(self.wallet,network=self.network)):
            raise StateError('another uncertain wallet operation blocks wallet unlock')
        indices={i['payment_index'] for i in intents}
        remaining=[(j,p) for j,p in enumerate(plan['payments']) if j not in indices]
        if not remaining: return {'plan':digest,'intents':intents,'settled':store.settlement(digest) is not None}
        self._funds(store,plan,intents,health)
        if confirm is not None: password=confirm(plan)
        if password is None: raise StateError('wallet password/confirmation required')
        # User prompts can take minutes. Revalidate health, identity, nonce and
        # funds immediately before unlock while retaining the same wallet lock.
        health=self.health(epoch=plan['epoch'] if kind=='payout' else None,wallet=True)
        if kind=='withdrawal':
            import owner_income
            owner_income.verify_auto_reservation(store,digest,health)
        self._check_plan(plan,health,kind); chain,pool=self._evidence(plan,health)
        self._verify_previous(store,health,current_digest=digest,next_epoch=plan['epoch'] if kind=='payout' else None)
        self._replay_guard(plan,chain,pool,intents)
        intents=self._refresh(store,plan,digest,chain,pool)
        if any(i['status'] in ('unknown','reserved') for i in intents): raise StateError('uncertainty appeared during confirmation; stop before unlock')
        self._funds(store,plan,intents,health)
        nonce=health['account']['inferred_nonce']
        try:
            self.adapter.unlock(password)
            for index,payment in remaining:
                intent=store.reserve_intent(digest,index,nonce)
                store.mark_unknown(intent['id'],'submission boundary entered; absence never authorizes retry')
                self._emit('sending',payment=payment,index=index,total=len(plan['payments']),nonce=nonce)
                response=self.adapter.submit(payment,nonce)
                try: receipt=command_identity(response['sendPayment']['payment'])
                except (KeyError,TypeError): raise StateError('malformed send response; outcome remains unknown') from None
                if not same_payment(intent,receipt) or not isinstance(receipt.get('tx_hash'),str) or not receipt['tx_hash']:
                    raise StateError('send receipt does not match durable intent; outcome unknown')
                store.mark_submitted(intent['id'],receipt['tx_hash']); nonce+=1
                self._emit('submitted',tx_hash=receipt['tx_hash'])
        except Exception as e:
            if isinstance(e,StateError): raise
            raise StateError('wallet operation failed or response lost; inspect journal and reconcile, never blindly retry') from None
        finally:
            try:
                self.adapter.lock()
                self._emit('locked')
            except Exception: raise StateError('wallet lock response failed; verify daemon wallet is locked before further operation') from None
        return {'plan':digest,'intents':store.list_intents(plan_digest=digest),'settled':False}
    def reconcile(self,digest,*,commit=False):
        with wallet_lock(self.state_dir,self.wallet):
            store=self.store(); plan=store.get_plan(digest); kind=plan.get('kind','payout')
            health=self.health(epoch=plan['epoch'] if kind=='payout' else None,wallet=True)
            self._check_plan(plan,health,kind); chain,pool=self._evidence(plan,health)
            intents=store.list_intents(plan_digest=digest); self._replay_guard(plan,chain,pool,intents)
            # Preview is truly read-only; assess fresh proof without journal transitions.
            if not commit: return {'plan':digest,'commit':False,'intents':intents,'chain_matches':[r for r in chain if any(same_payment(i,r) for i in intents)]}
            intents=self._refresh(store,plan,digest,chain,pool)
            self._emit('proofs',plan=plan,intents=intents,chain=chain,pool=pool,required_depth=self.tx_depth(plan))
            if len(intents)!=len(plan['payments']) or any(i['status'] not in ('confirmed','failed') for i in intents): raise PendingPayments('pending/unknown or unsent payments: no financial commit')
            existing=store.settlement(digest)
            data=self._checked_ledger(store,recover_digest=digest if existing is None and kind=='payout' else None)
            if kind=='withdrawal' and existing is not None:
                return {'plan':digest,'balances_nano':existing,'already_settled':True}
            balances=self.carry() if kind=='withdrawal' else self._payout_balances(plan,intents)
            if existing is not None:
                if existing!=balances: raise StateError('settled accounting mismatch')
                import owner_income
                owner_income.record_payout(store,digest,plan,intents,self.config,health['account']['nonce'])
                return {'plan':digest,'balances_nano':balances,'already_settled':True}
            if data is not None and kind=='payout':
                entries=[h for h in data['history'] if h.get('plan_digest')==digest]
                if not entries:
                    current={a:mina_to_nano(v,allow_negative=True) for a,v in data['balances'].items()}
                    if current!=plan['carry_before_nano']: raise StateError('ledger changed after plan snapshot')
                    import ledger
                    data['balances']={a:nano_to_mina(n) for a,n in balances.items()}
                    data['history'].append(self._ledger_entry(plan,digest,balances))
                    ledger.save(data,self.root/'payout_ledger.json')
            store.settle(digest,balances)
            import owner_income
            owner_income.record_payout(store,digest,plan,intents,self.config,health['account']['nonce'])
            return {'plan':digest,'balances_nano':balances,'already_settled':False}
    def withdraw(self,recipient,amount_nano,*,password=None,confirm=None,dry_run=False,operation_id=None,reuse_existing=False,new_operation=False):
        import owner_income
        validate_address(recipient)
        automatic=amount_nano is None
        if not automatic: integer(amount_nano,'withdrawal amount',1)
        with wallet_lock(self.state_dir,self.wallet):
            store=self.store(); self._unsettled(store)
            health=self.health(wallet=True)
            if not dry_run: self._sync_owner_income(store,health['account']['nonce'])
            income=owner_income.summary(store,self.wallet,self.network)
            if automatic:
                if new_operation: raise StateError('--new-operation требует явно заданной суммы; автоматический вывод использует только остаток дохода.')
                pending=[(d,p) for d,p in store.list_plans(self.wallet,self.network) if p.get('kind')=='withdrawal' and store.settlement(d) is None]
                chosen=[(d,p) for d,p in store.list_plans(self.wallet,self.network) if p.get('operation_id')==operation_id] if operation_id else pending
                if chosen:
                    if len(chosen)!=1 or chosen[0][1]['payments'][0]['recipient']!=recipient:
                        raise StateError('Продолжите незавершённый вывод с исходным получателем.')
                    with store._connection() as db:
                        source=db.execute('SELECT source FROM owner_withdrawals WHERE plan_digest=?',(chosen[0][0],)).fetchone()
                    if (source[0] if source else chosen[0][1].get('withdrawal_source'))!='auto':
                        raise StateError('Незавершённый вывод задан вручную; продолжите его с явной суммой --amount и исходным --operation-id.')
                    operation_id=chosen[0][1]['operation_id']
                    amount_nano=chosen[0][1]['payments'][0]['amount_nano']
                elif operation_id:
                    raise StateError('unknown withdrawal operation ID')
                else:
                    owner_income.check_external_spends(store,self.wallet,self.network,health,income)
                    amount_nano=income['available_nano']-integer(self.config['DEFAULT_TX_FEE'],'fee')
                    if amount_nano<=0:
                        self._emit('income',summary=income,amount_nano=0,fee_nano=self.config['DEFAULT_TX_FEE'])
                        return {'nothing_to_withdraw':True,'owner_income':income,'dry_run':dry_run}
                    operation_id=str(uuid.uuid4())
            self._emit('income',summary=income,amount_nano=amount_nano,fee_nano=self.config['DEFAULT_TX_FEE'])
            if new_operation and operation_id is not None:
                raise StateError('choose an existing operation ID or --new-operation, not both')
            if reuse_existing and operation_id is None:
                withdrawals=[(d,p) for d,p in store.list_plans(self.wallet,self.network) if p.get('kind')=='withdrawal']
                pending=[(d,p) for d,p in withdrawals if store.settlement(d) is None]
                matches=lambda p: len(p['payments'])==1 and p['payments'][0]['recipient']==recipient and p['payments'][0]['amount_nano']==amount_nano
                if pending:
                    if new_operation or len(pending)!=1 or not matches(pending[0][1]):
                        raise StateError('another withdrawal remains unsettled; continue its original amount/recipient before starting a new one')
                    operation_id=pending[0][1]['operation_id']
                elif not new_operation:
                    # A repeated command after a lost final response must not
                    # silently initiate another payment of the same amount.
                    matching=[p for _,p in withdrawals if matches(p)]
                    if matching: operation_id=matching[-1]['operation_id']
            operation_id=operation_id or str(uuid.uuid4())
            existing=[(d,p) for d,p in store.list_plans(self.wallet,self.network) if p.get('operation_id')==operation_id]
            plan={'wallet':self.wallet,'network':self.network,'era':str(health['era']),'epoch':health['current_epoch'],'kind':'withdrawal','operation_id':operation_id,'withdrawal_source':'auto' if automatic else 'manual',
                  'min_confirmations':integer(self.config.get('TX_CONFIRMATIONS_NUM',3),'TX_CONFIRMATIONS_NUM',1),
                  'payments':[{'recipient':recipient,'amount_nano':amount_nano,'fee_nano':integer(self.config['DEFAULT_TX_FEE'],'fee'),'memo':str(self.config.get('WITHDRAW_MEMO',''))}]}
            if existing:
                digest,old=existing[0]
                if old['payments']!=plan['payments']: raise StateError('withdrawal operation identity already has different immutable inputs')
                plan=old
            elif dry_run:
                self._verify_previous(store,health)
                self._check_plan(plan,health,'withdrawal')
                return dict(self._funds(store,plan,[],health),dry_run=True,operation_id=operation_id,amount_nano=amount_nano,fee_nano=plan['payments'][0]['fee_nano'],owner_income=income)
            else:
                if any(i['status'] not in ('confirmed','failed') for i in store.list_intents(self.wallet,network=self.network)): raise StateError('unresolved wallet operation blocks new withdrawal')
                self._verify_previous(store,health)
                self._check_plan(plan,health,'withdrawal'); self._funds(store,plan,[],health)
                digest=store.create_plan(plan); store.bind_plan(digest)
            if not dry_run:
                owner_income.initialize_account(store,self.wallet,self.network,health['account']['nonce'])
                owner_income.reserve_withdrawal(store,digest,plan.get('withdrawal_source','manual'))
            return self._send(digest,password=password,confirm=confirm,dry_run=dry_run,kind='withdrawal')
