"""Validator income and withdrawal reservations in the private payout journal.

Every amount is integer nanomina. Call mutations under the shared wallet lock.
Credits are immutable; withdrawal debits follow durable transaction outcomes.
"""
import json
from payout_state import StateError, _canonical, _hash, _integer, _now


TABLES = (
    '''CREATE TABLE owner_accounts(wallet TEXT NOT NULL,network TEXT NOT NULL,
       baseline_nonce INTEGER NOT NULL,created TEXT NOT NULL,PRIMARY KEY(wallet,network))''',
    '''CREATE TABLE owner_earnings(wallet TEXT NOT NULL,network TEXT NOT NULL,era TEXT NOT NULL,
       epoch INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,created TEXT NOT NULL,
       PRIMARY KEY(wallet,network,era,epoch))''',
    '''CREATE TABLE owner_withdrawals(plan_digest TEXT PRIMARY KEY REFERENCES plans(digest),
       source TEXT NOT NULL,created TEXT NOT NULL)''',
)


def initialize_account(store,wallet,network,nonce):
    _integer(nonce,'income baseline nonce')
    with store._connection() as db:
        db.execute('INSERT OR IGNORE INTO owner_accounts VALUES (?,?,?,?)',(wallet,network,nonce,_now()))


def record_credit(store,record):
    for key in ('validator_nano','retained_own_nano','payout_fees_nano','own_paid_nano'):
        _integer(record.get(key),key)
    _integer(record.get('epoch'),'income epoch')
    if not all(isinstance(record.get(k),str) and record[k] for k in ('wallet','network','era')):
        raise StateError('income identity is required')
    amounts=record.get('own_retained_by_address',{})
    if not isinstance(amounts,dict): raise StateError('own income breakdown must be an object')
    for value in amounts.values(): _integer(value,'own income')
    if amounts and sum(amounts.values())!=record['retained_own_nano']:
        raise StateError('own income breakdown does not match total')
    payload=_canonical(record); digest=_hash(payload.encode())
    identity=tuple(record[k] for k in ('wallet','network','era','epoch'))
    with store._connection() as db:
        old=db.execute('SELECT payload FROM owner_earnings WHERE wallet=? AND network=? AND era=? AND epoch=?',identity).fetchone()
        if old and old[0]!=payload: raise StateError('income credit already exists with different evidence')
        db.execute('INSERT OR IGNORE INTO owner_earnings VALUES (?,?,?,?,?,?,?)',(*identity,payload,digest,_now()))


def reserve_withdrawal(store,digest,source):
    plan=store.get_plan(digest)
    if plan.get('kind')!='withdrawal': raise StateError('income debit must reference a withdrawal')
    if source not in ('auto','manual'): raise StateError('invalid withdrawal source')
    if plan.get('withdrawal_source',source)!=source: raise StateError('withdrawal source differs from immutable plan')
    with store._connection() as db:
        old=db.execute('SELECT source FROM owner_withdrawals WHERE plan_digest=?',(digest,)).fetchone()
        if old and old[0]!=source: raise StateError('withdrawal reservation source mismatch')
        db.execute('INSERT OR IGNORE INTO owner_withdrawals VALUES (?,?,?)',(digest,source,_now()))


def summary(store,wallet,network):
    with store._connection() as db:
        rows=db.execute('SELECT payload,digest FROM owner_earnings WHERE wallet=? AND network=? ORDER BY era,epoch',(wallet,network)).fetchall()
        debits=[r[0] for r in db.execute('SELECT plan_digest FROM owner_withdrawals')]
        account=db.execute('SELECT baseline_nonce FROM owner_accounts WHERE wallet=? AND network=?',(wallet,network)).fetchone()
    credits=[]
    for payload,digest in rows:
        if _hash(payload.encode())!=digest: raise StateError('income evidence checksum mismatch')
        credits.append(json.loads(payload))
    spent=reserved=0
    for digest in debits:
        plan=store.get_plan(digest)
        if (plan['wallet'],plan['network'])!=(wallet,network): continue
        value=sum(p['amount_nano']+p['fee_nano'] for p in plan['payments'])
        intents=store.list_intents(plan_digest=digest)
        if len(intents)==len(plan['payments']) and all(i['status']=='confirmed' for i in intents): spent+=value
        elif intents and all(i['status']=='failed' for i in intents): pass
        else: reserved+=value
    total=sum(r['validator_nano']+r['retained_own_nano']-r['payout_fees_nano'] for r in credits)
    return {'credits':credits,'earned_nano':total,'withdrawn_with_fees_nano':spent,
            'reserved_with_fees_nano':reserved,'available_nano':total-spent-reserved,
            'baseline_nonce':account[0] if account else None}


def check_external_spends(store,wallet,network,health,account):
    baseline=account['baseline_nonce']
    if baseline is None: raise StateError('Учёт дохода ещё не инициализирован.')
    current=health['account']['nonce']
    if current<baseline: raise StateError('Nonce ниже начала учёта доходов; проверьте реорганизацию цепочки.')
    intents={i['nonce']:i for i in store.list_intents(wallet,network=network) if baseline<=i['nonce']<current}
    if len(intents)!=current-baseline or any(i['status']=='failed' for i in intents.values()):
        raise StateError('После начала учёта найден неучтённый исходящий перевод; автоматический вывод остановлен.')


def verify_auto_reservation(store,digest,health):
    with store._connection() as db:
        row=db.execute('SELECT source FROM owner_withdrawals WHERE plan_digest=?',(digest,)).fetchone()
    plan=store.get_plan(digest)
    if plan.get('withdrawal_source')=='auto' and (not row or row[0]!='auto'):
        raise StateError('automatic withdrawal reservation is missing or inconsistent')
    if row and row[0]=='auto':
        account=summary(store,plan['wallet'],plan['network'])
        check_external_spends(store,plan['wallet'],plan['network'],health,account)
        if account['available_nano']<0:
            raise StateError('Зарезервированный вывод превышает учтённый доход; отправка остановлена.')


def own_addresses(config):
    return {a for a in [config.get('SEND_FROM_ADDRESS'),config.get('VALIDATOR_ADDRESS'),
                       config.get('WITHDRAW_TO_ADDRESS'),*config.get('EXCLUDE_FROM_PAYOUTS',[])] if a}


def record_payout(store,digest,plan,intents,config,nonce):
    if plan.get('kind','payout')!='payout': return
    c=plan.get('calculation',{})
    if not all(k in c for k in ('validator_nano','excluded_nano','total_reward_nano')):
        return  # Compatibility fixtures/older incomplete plans need explicit import.
    if store.settlement(digest) is None: raise StateError('income is credited only after payout settlement')
    # Idempotency also freezes the ownership interpretation used at first credit.
    with store._connection() as db:
        old=db.execute('SELECT 1 FROM owner_earnings WHERE wallet=? AND network=? AND era=? AND epoch=?',
                       (plan['wallet'],plan['network'],plan['era'],plan['epoch'])).fetchone()
    if old: return
    own=own_addresses(config)
    by_address=c.get('excluded_by_address_nano',{})
    if not by_address and c['excluded_nano'] and not set(c['policy']['exclusions'])<=own:
        raise StateError('cannot attribute excluded income to configured own addresses')
    retained={a:n for a,n in by_address.items() if a in own}
    record={'wallet':plan['wallet'],'network':plan['network'],'era':plan['era'],'epoch':plan['epoch'],
            'validator_nano':c['validator_nano'],
            'retained_own_nano':sum(retained.values()) if by_address else c['excluded_nano'],
            'own_retained_by_address':retained,
            'own_paid_nano':sum(i['payment']['amount_nano'] for i in intents if i['status']=='confirmed' and i['payment']['recipient'] in own),
            'payout_fees_nano':sum(i['payment']['fee_nano'] for i in intents if i['status']=='confirmed'),
            'source':{'kind':'settled_plan','plan_digest':digest}}
    initialize_account(store,plan['wallet'],plan['network'],nonce)
    record_credit(store,record)
