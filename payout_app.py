"""CLI-facing read-only reports, exact calculation and compatibility exports."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from money import mina_to_nano, nano_to_mina
from payout_state import StateError, wallet_lock
from payout_calculation import calculate
from payout_workflow import read_legacy_receipts, read_csv, same_payment, fingerprint


def legacy_status(workflow,epoch,*,online=False):
    path=workflow.root/f'sended_txs_e{epoch}.csv'
    rows=read_legacy_receipts(path) if path.exists() else []
    ledger=workflow._ledger()
    histories=[h for h in (ledger['history'] if ledger else []) if h['epoch']==epoch and 'plan_digest' not in h]
    result={'epoch':epoch,'era':str(workflow.config.get('LEGACY_PROTOCOL_VERSION_ID','unbound')),
            'read_only':True,'sent_count':len(rows),'sent_nano':sum(r['amount_nano'] for r in rows),
            'legacy_reconciled':bool(histories),'verification':'local receipts only'}
    if online:
        health=workflow.health(epoch=epoch,wallet=False)
        if str(health['era'])!=result['era']: raise StateError('legacy era differs from live era; explicit historical verification required')
        chain=workflow.adapter.payments(health,epoch=epoch); proofs=[]
        required=int(workflow.config.get('TX_CONFIRMATIONS_NUM',workflow.config.get('CONFIRMATIONS_NUM',20)))
        for r in rows:
            intent={'wallet':r['wallet'],'nonce':r['nonce'],'payment':{k:r[k] for k in ('recipient','amount_nano','fee_nano','memo')}}
            matches=[p for p in chain if same_payment(intent,p) and (not r['tx_hash'] or p['tx_hash']==r['tx_hash'])]
            proofs.append({'nonce':r['nonce'],'matched':len(matches)==1,'depth':matches[0]['depth'] if len(matches)==1 else None,
                           'confirmed':len(matches)==1 and matches[0]['depth']>=required,'tx_hash':matches[0]['tx_hash'] if len(matches)==1 else None})
        result.update(verification='fresh applied best-chain identities',payments=proofs,min_confirmations=required)
    return result


def live_block_report(workflow,epoch,health):
    import GraphQL
    GraphQL.configure(workflow.config,health)
    return GraphQL.getBlockStatusReport(workflow.config['VALIDATOR_ADDRESS'],epoch)


def wallet_summary(workflow,health):
    return {'liquid_nano':health['account']['liquid_nano'],
            'mempool_pending_nano':health['mempool_pending_nano'],
            'carried_obligations_nano':sum(max(0,n) for n in workflow.carry().values())}


def plan_status(store,digest):
    intents=store.list_intents(plan_digest=digest)
    counts={}
    for i in intents: counts[i['status']]=counts.get(i['status'],0)+1
    return {'counts':counts,'settled':store.settlement(digest) is not None,
            'by_recipient':{i['payment']['recipient']:i['status'] for i in intents}}


def live_inputs(workflow,epoch,health):
    import GraphQL
    GraphQL.configure(workflow.config,health)
    ledger_hash=GraphQL.getLedgerHash(epoch)
    stakes=GraphQL.getStakingLedger({'delegate':workflow.config['VALIDATOR_ADDRESS'],'ledgerHash':ledger_hash})['data']['stakes']
    report=GraphQL.getBlockStatusReport(workflow.config['VALIDATOR_ADDRESS'],epoch)
    minimum=int(workflow.config.get('FIRST_BLOCK_HEIGHT',0)) or health.get('epoch_first_height')
    if minimum is None: minimum=min(b['height'] for b in health['best_chain'])
    maximum=health['tip_height']-int(workflow.config.get('CONFIRMATIONS_NUM',20))
    configured_max=int(workflow.config.get('LATEST_BLOCK_HEIGHT',0))
    if configured_max: maximum=min(maximum,configured_max)
    rows=GraphQL.getBlocks({'creator':workflow.config['VALIDATOR_ADDRESS'],'epoch':epoch,'blockHeightMin':minimum,'blockHeightMax':maximum})['data']['blocks']
    ancestry={b['state_hash']:b for b in health['best_chain']}; seen=set(); blocks=[]
    for b in rows:
        if b['stateHash'] not in ancestry or b['stateHash'] in seen: raise StateError('reward input is duplicate or outside captured best chain')
        seen.add(b['stateHash'])
        blocks.append({'reward_nano':int(b['transactions']['coinbase']),'snark_nano':int(b['snarkFees']),'fees_nano':int(b['txFees']),
                       'slot':b['protocolState']['consensusState']['slotSinceGenesis'],'winner':b['winnerAccount']['publicKey'],
                       'state_hash':b['stateHash'],'height':b['blockHeight']})
    expected={b['state_hash'] for b in report['blocks'] if b['mature'] and minimum<=b['height']<=maximum}
    if expected!=seen: raise StateError('reward query and authoritative producer block report disagree')
    def addresses(name):
        path=workflow.root/name
        if not path.is_file(): raise StateError('required delegator classification file is missing: '+name)
        return sorted({line.strip() for line in path.read_text().splitlines() if line.strip()})
    return {'stakes':stakes,'blocks':blocks,'ledger_hash':ledger_hash,'base_coinbase_nano':mina_to_nano(GraphQL.getBaseCoinbase()),
            'foundation':addresses('foundation_addresses.txt'),'labs':addresses('O(1)Labs.txt'),'report':report}


def _export(path,amounts):
    content=''.join(f'{a};{n};{nano_to_mina(n):.9f}\n' for a,n in sorted(amounts.items()))
    # Exports are compatibility views. Journal immutable snapshot is authority.
    if path.exists():
        if path.read_text()!=content: raise StateError('refusing to overwrite existing compatibility CSV')
        return
    fd,tmp=tempfile.mkstemp(prefix='.'+path.name,dir=path.parent)
    try:
        with os.fdopen(fd,'w') as f: f.write(content); f.flush(); os.fsync(f.fileno())
        os.link(tmp,path)  # exclusive creation; no replace of concurrent legacy artifact
        directory=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally: os.unlink(tmp)


def calculate_epoch(workflow,epoch,*,preview=False,provider=live_inputs):
    with wallet_lock(workflow.state_dir,workflow.wallet):
        # Resolve live era before interpreting an epoch number reused at a fork.
        health=workflow.health(epoch=epoch,wallet=False)
        if workflow._legacy_epoch(epoch,health['era']):
            if preview: raise StateError('legacy policy is not reconstructible from current config; use recorded historical artifacts')
            return {'legacy':legacy_status(workflow,epoch),'preview':True}
        preview=preview or epoch>=health['current_epoch']
        store=workflow.store()
        old=[(d,p) for d,p in store.list_plans(workflow.wallet,workflow.network) if p['epoch']==epoch and p['era']==str(health['era']) and p.get('kind','payout')=='payout']
        if old and not preview:
            digest,plan=old[0]
            workflow._check_plan(plan,health)
            return {'epoch':epoch,'plan':digest,'calculation':plan['calculation'],'preview':False,'existing':True,
                    'wallet':wallet_summary(workflow,workflow.health(wallet=True)),
                    'block_report':live_block_report(workflow,epoch,health),
                    'reward_blocks':plan['inputs'].get('blocks',[]),'staking':plan['inputs'].get('stakes',[]),
                    'payment_status':plan_status(store,digest)}
        data=provider(workflow,epoch,health)
        if not preview and any(not b['lost'] and not b['mature'] for b in data['report']['blocks']):
            raise StateError('closed epoch still has immature producer blocks; wait before freezing payout plan')
        carry=workflow.carry()
        calculation=calculate(workflow.config,health['era'],epoch,data['stakes'],data['blocks'],base_coinbase_nano=data['base_coinbase_nano'],carry=carry,foundation=data['foundation'],labs=data['labs'])
        calculation['policy'].update({key:int(workflow.config.get(key,default)) for key,default in [('FIRST_BLOCK_HEIGHT',0),('LATEST_BLOCK_HEIGHT',0),('CONFIRMATIONS_NUM',20)]})
        inputs={key:data[key] for key in ('ledger_hash','stakes','blocks','base_coinbase_nano','foundation','labs')}
        wallet_health=workflow.health(wallet=True)
        wallet=wallet_summary(workflow,wallet_health)
        result={'epoch':epoch,'era':str(health['era']),'preview':preview,'calculation':calculation,'block_report':data['report'],'wallet':wallet,'staking':data['stakes'],'reward_blocks':data['blocks']}
        if not preview:
            # Existing old CSV cannot silently become the new plan's send source.
            old_namespace=str(health['era'])==str(workflow.config.get('LEGACY_PROTOCOL_VERSION_ID'))
            if old_namespace and any((workflow.root/f'e{epoch}_{suffix}.csv').exists() for suffix in ('payouts','owed')):
                raise StateError('legacy calculation exists; explicit verified import or separate preview is required')
            digest=workflow._create_plan(epoch,calculation,inputs,health=health)
            for suffix,amounts in [('owed',calculation['gross_nano']),('payouts',calculation['net_nano'])]:
                _export(workflow.root/f"era{health['era']}_e{epoch}_{suffix}.csv",amounts)
                compatibility=workflow.root/f'e{epoch}_{suffix}.csv'
                if not compatibility.exists(): _export(compatibility,amounts)
            result['plan']=digest
        return result


def report(result):
    """Human report; frozen calculation and current wallet are labelled separately."""
    from payout_display import mina, paint, account_link, status_tone
    from tabulate import tabulate
    if 'legacy' in result:
        r=result['legacy']
        print(f"Эпоха {r['epoch']} · исторические выплаты (только чтение)")
        print(f"Отправлено: {r['sent_count']} платежей · {mina(r['sent_nano'])} MINA")
        print('Учёт сверен: '+('да' if r['legacy_reconciled'] else 'нет'))
        return
    c=result['calculation']; policy=c['policy']; status=result.get('payment_status',{})
    print(f"\nЭпоха {result.get('epoch','—')} · расчёт выплат")
    if result['preview']: print('Предварительный расчёт: эпоха ещё идёт, отправка недоступна.')
    elif result.get('existing'): print('Сохранённый расчёт — суммы не пересчитывались.')
    else: print('Расчёт сохранён. Суммы зафиксированы для отправки.')
    if status:
        print('Учёт выплат: '+('завершён' if status['settled'] else 'ещё не завершён'))
        print('Статусы платежей ниже — по локальному журналу; свежую глубину проверяет mina_pay.')
    print(paint('\nКошелёк сейчас','cyan'))
    if 'wallet' in result:
        w=result['wallet']
        print(f"  Доступный баланс ноды: {mina(w['liquid_nano'])} MINA")
        print(f"  В ожидании отправки из кошелька (пул ноды): {mina(w['mempool_pending_nano'])} MINA")
        print(f"  Долги делегаторам в текущем учёте: {mina(w['carried_obligations_nano'])} MINA")
        print('  Баланс включает обязательства по выплатам и не равен свободной комиссии валидатора.')
    else: print('  Нет свежих данных.')
    print(paint('\nБлоки валидатора','cyan'))
    reward_heights={b['height'] for b in result.get('reward_blocks',[])}
    rows=[]
    for b in result.get('block_report',{}).get('blocks',[]):
        label=('потерян, не учтён' if b['lost'] else 'ожидает глубины' if not b['mature']
               else 'учтён в расчёте' if b.get('height') in reward_heights else 'вне расчёта')
        rows.append([b.get('height','—'),b.get('coinbase_mina','—'),b.get('confirmations','—'),paint(label,'red' if b['lost'] else 'green' if label=='учтён в расчёте' else 'yellow')])
    print(tabulate(rows,headers=['Высота','Награда блока MINA','Блоков после','Результат'],tablefmt='simple',disable_numparse=True) if rows else '  Нет блоков в отчёте.')
    print('\nРаспределение за эпоху (сохранённый расчёт)' if result.get('existing') else '\nРаспределение за эпоху')
    for label,key in [('Награды с учётом tx/SNARK-комиссий','total_reward_nano'),
                      ('Доля валидатора, включая округление','validator_nano'),
                      ('Доля собственных/исключённых адресов','excluded_nano')]:
        print(f"  {label}: {mina(c.get(key,0))} MINA")
    from decimal import Decimal
    rates=[('обычная','VALIDATOR_FEE'),('supercharged','VALIDATOR_FEE_SP'),('Foundation','VALIDATOR_FEE_FOUNDATION'),('O(1)Labs','VALIDATOR_FEE_O1LABS')]
    print('  Комиссия валидатора: '+', '.join(name+' '+format(Decimal(policy[key])*100,'f')+'%' for name,key in rates if key in policy))
    print(paint('\nВыплаты делегаторам · все суммы в MINA','cyan'))
    print('Начислено + Долг до = К выплате + Остаток после. Остаток переносится на будущие выплаты.')
    labels={'submitted':'отправлен','confirmed':'подтверждён','failed':'не выполнен',
            'unknown':'исход неизвестен','reserved':'зарезервирован'}
    rows=[]
    for a in sorted(set(c['gross_nano'])|set(c['carry_before_nano'])|set(c['net_nano'])):
        gross=c['gross_nano'].get(a,0); before=c['carry_before_nano'].get(a,0)
        send=c['net_nano'].get(a,0); after=c['carry_after_nano'].get(a,0)
        label=labels.get(status.get('by_recipient',{}).get(a))
        if label is None:
            label='к отправке' if send else 'ниже минимума' if after>0 and after<policy.get('minimum_nano',0) else 'нет выплаты'
        rows.append([account_link(a),mina(gross),mina(before),mina(send),mina(after),paint(label,status_tone(status.get('by_recipient',{}).get(a)))])
    print(tabulate(rows,headers=['Адрес получателя','Начислено','Долг до','К выплате','Остаток после','Статус'],tablefmt='simple',disable_numparse=True,colalign=('left','right','right','right','right','left')) if rows else '  Получателей нет.')
    total=sum(c['net_nano'].values()); fees=len(c['net_nano'])*policy['fee_nano']
    print(f"\nПо плану: {len(c['net_nano'])} платежей · {mina(total)} MINA")
    print(f"Сетевые комиссии: {mina(fees)} MINA · Всего с комиссиями: {mina(total+fees)} MINA")
    print('Минимальная выплата: '+mina(policy.get('minimum_nano',0))+' MINA')
    print('Плановый остаток долга после успешных выплат: '+mina(sum(max(0,n) for n in c['carry_after_nano'].values()))+' MINA')
    print('При неуспешном платеже его сумма остаётся долгом; окончательный остаток определяет сверка.')
    stakes=result.get('staking',[])
    if stakes:
        last_slot=max((b['slot'] for b in result.get('reward_blocks',[])),default=0)
        rows=[]
        for s in stakes:
            timing=s.get('timing')
            locked=bool(timing and int(timing['untimed_slot'])>last_slot)
            rows.append([account_link(s['public_key']),str(s['balance']),'есть' if locked else 'нет',
                         'исключён из выплат' if s['public_key'] in policy.get('exclusions',[]) else 'участвует'])
        print(paint('\nДелегации из staking ledger расчёта · вестинг на слот последнего учтённого блока','cyan'))
        print(tabulate(rows,headers=['Адрес делегатора','Стейк MINA','Вестинг','Участие'],tablefmt='simple',disable_numparse=True))
    if policy.get('redirects'):
        print('Перенаправления выплат (исходный адрес → получатели и веса):')
        for source,targets in policy['redirects'].items():
            print('  '+account_link(source)+' → '+', '.join(account_link(destination)+' (вес '+str(weight)+')' for destination,weight in targets))
    if result.get('plan'): print('\nИдентификатор плана: '+result['plan'])


def vrf_report(workflow,epoch,*,refresh=False):
    """Explicit opt-in retains original VRF scan/cache behavior; never in tests."""
    import GraphQL
    from vrf import probe
    ledger_hash=GraphQL.getLedgerHash(epoch)
    data=None if refresh else probe.load_cached(epoch,ledger_hash=ledger_hash)
    if data is None: data=probe.scan_epoch(epoch)
    return data
