"""Explicit mainnet batch operations, isolated from existing payout plans.

The caller holds the normal payout wallet lock across signing/claim/submission.
Only the separate batch journal is mutated. Ordinary payout accounting is read,
never initialized, settled or rewritten by this command.
"""
import argparse
import getpass
import json
from pathlib import Path
import time
import urllib.request
import warnings

from batch_accounting import BatchStore, FILENAME, canonical_plan, readonly_snapshot
from payout_state import StateError, atomic_write_json, wallet_lock

PROJECT=Path(__file__).resolve().parent


def signer_packet(plan,index,nonce):
    return {'network':'mainnet','wallet':plan['wallet'],'nonce':nonce,
            'fee_nano':str(plan['fee_nano']),'memo':plan['memo'],
            'recipients':[{'recipient':r['recipient'],'amount_nano':str(r['amount_nano'])}
                          for r in plan['batches'][index]['recipients']]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise StateError('Batch submission redirect refused')


def submit_once(config,query,expected_hash):
    """Exactly one HTTP mutation. Any uncertain result remains reserved."""
    host=config.get('GRAPHQL_HOST','127.0.0.1')
    if host not in ('127.0.0.1','localhost','::1'):raise StateError('Batch requires the local daemon tunnel')
    url='{}://{}:{}/graphql'.format(config.get('GRAPHQL_SCHEME','http'),'[::1]' if host=='::1' else host,config.get('GRAPHQL_PORT',3085))
    request=urllib.request.Request(url,data=json.dumps({'query':query}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request,timeout=30) as response:
            body=json.load(response)
        tx=body.get('data',{}).get('sendZkapp',{}).get('zkapp',{})
        if body.get('errors') or tx.get('hash')!=expected_hash or tx.get('failureReason'):
            raise ValueError('not acknowledged')
        return expected_hash
    except Exception:
        raise StateError('Исход отправки неизвестен. Пакет сохранён; продолжайте только сверку, повторной отправки нет.') from None


class BatchWorkflow:
    def __init__(self,store,adapter):self.store,self.adapter=store,adapter

    @staticmethod
    def _identity(plan,health):
        if health.get('network')!=plan['network'] or str(health.get('era'))!=plan['era']:
            raise StateError('Batch network or era changed')

    def reconcile(self,digest,required_depth=3):
        plan=self.store.get_plan(digest)
        if self.adapter.health().get('network')!=plan['network']:
            raise StateError('Batch network changed')
        observed=[]
        for row in self.store.rows(digest):
            if row['status'] in ('planned','prepared','cancelled'):continue
            expected=self.store.expected(digest,row['index'])
            validated=self.adapter.validate(json.dumps(row['signed']),signer_packet(plan,row['index'],row['nonce']))
            if validated['tx_hash']!=row['tx_hash']:raise StateError('Stored batch hash differs from signed transaction')
            proof=self.adapter.proof(expected,required_depth=required_depth)
            if row['status'] in ('confirmed','failed'):
                status='applied' if row['status']=='confirmed' else 'failed'
                if proof.get('status')!=status or proof.get('depth',-1)<required_depth:
                    raise StateError('Previously confirmed batch lost its fresh proof')
            if proof.get('status') in ('applied','failed'):
                self.store.observe(digest,row['index'],proof,required_depth=required_depth)
            observed.append({'index':row['index'],**proof})
        if any(r['status']=='failed' for r in self.store.rows(digest)):
            self.store.cancel_unsigned_after_failure(digest)
        return observed

    def send_next(self,digest,password_provider):
        plan=self.store.get_plan(digest)
        for other,p in self.store.list_plans(plan['wallet'],plan['network']):
            if other!=digest:
                self.reconcile(other,required_depth=15)
                if any(r['status'] not in ('confirmed','failed','cancelled') for r in self.store.rows(other)):
                    raise StateError('A previous batch operation is unfinished')
        had_unknown=any(r['status']=='unknown' for r in self.store.rows(digest))
        self.reconcile(digest)
        rows=self.store.rows(digest)
        if any(r['status']=='failed' for r in rows):raise StateError('Batch failed in chain; automatic retry and next-batch sending are disabled')
        if had_unknown or any(r['status']=='unknown' for r in rows):return rows
        row=next((r for r in rows if r['status']!='confirmed'),None)
        if row is None:return rows
        health=self.adapter.approve(plan,digest);self._identity(plan,health)
        nonce=health['account']['nonce']
        if row['status']=='planned':
            packet=signer_packet(plan,row['index'],nonce)
            unsigned=self.adapter.build(packet)
            signed=self.adapter.sign(unsigned,packet,password_provider())
            self.store.prepare(digest,row['index'],nonce,signed['signed'],signed['tx_hash'])
        row=self.store.rows(digest)[row['index']]
        packet=signer_packet(plan,row['index'],row['nonce'])
        verified=self.adapter.validate(json.dumps(row['signed']),packet)
        if verified['tx_hash']!=row['tx_hash']:raise StateError('Signed transaction hash mismatch')
        # Password entry and signing can take arbitrarily long. Refresh batch
        # ancestry after that wait as well as ordinary payment proofs below.
        for previous,_ in self.store.list_plans(plan['wallet'],plan['network']):
            self.reconcile(previous,required_depth=3 if previous==digest else 15)
        fresh=self.adapter.approve(plan,digest);self._identity(plan,fresh)
        if fresh['account']['nonce']!=row['nonce']:raise StateError('Nonce changed after signing; original intent retained')
        if not self.store.claim(digest,row['index']):return self.store.rows(digest)
        returned=self.adapter.submit(json.dumps(row['signed']),self.store.expected(digest,row['index']))
        if returned!=row['tx_hash']:raise StateError('Submission hash mismatch; original batch remains unknown')
        return self.store.rows(digest)


class MainnetAdapter:
    def __init__(self,config,root=PROJECT):
        from payout_workflow import Workflow
        self.config=config;self.root=Path(root);self.legacy=Workflow(config,self.root,readonly=True)
        self.state_dir=self.legacy.state_dir

    def health(self,epoch=None):
        import preflight
        # The ordinary preflight intentionally rejects unknown zkApp nonce gaps.
        # The batch adapter independently checks both command pools below.
        for attempt in range(3):
            try:h=preflight.check(self.config,require_wallet=False,**({'epoch':epoch} if epoch is not None else {}));break
            except preflight.PreflightArchiveLagError:
                if attempt==2:raise
                print('Архив догоняет свежий блок; повторяю только проверку…',flush=True);time.sleep(15)
        data=preflight._graphql(self.config,'''query($pk:PublicKey!){signatureKind daemonStatus{chainId}
          account(publicKey:$pk){nonce inferredNonce balance{total liquid}}
          pooledUserCommands(publicKey:$pk){id hash nonce from to amount fee memo isDelegation}
          pooledZkappCommands(publicKey:$pk){hash zkappCommand{feePayer{body{publicKey fee nonce}}}}}''',{'pk':self.config['SEND_FROM_ADDRESS']})
        if data.get('signatureKind')!='mainnet' or data.get('daemonStatus',{}).get('chainId')!=h['network']:
            raise StateError('Mainnet signing domain or chain identity changed')
        a=data.get('account')
        if not a:raise StateError('Sender account missing')
        integer=preflight._integer
        account={'nonce':integer(a['nonce'],'nonce'),'inferred_nonce':integer(a['inferredNonce'],'inferred nonce'),
                 'liquid_nano':integer(a['balance']['liquid'],'liquid'),'balance_nano':integer(a['balance']['total'],'balance')}
        ordinary=[p for p in data['pooledUserCommands'] if p['from']==self.config['SEND_FROM_ADDRESS']]
        zkapps=[p for p in data['pooledZkappCommands'] if p['zkappCommand']['feePayer']['body']['publicKey']==self.config['SEND_FROM_ADDRESS']]
        nonces=[integer(p['nonce'],'pool nonce') for p in ordinary]+[integer(p['zkappCommand']['feePayer']['body']['nonce'],'zkApp nonce') for p in zkapps]
        if account['inferred_nonce']<account['nonce'] or sorted(nonces)!=list(range(account['nonce'],account['inferred_nonce'])):
            raise StateError('Wallet nonce and both transaction pools disagree')
        h.update(account=account,pooled_commands=ordinary,pooled_zkapps=zkapps,
                 mempool_pending_nano=sum(integer(p['fee'],'pool fee')+(0 if p['isDelegation'] else integer(p['amount'],'pool amount')) for p in ordinary))
        return h

    def inspect(self,plan,digest=None):
        """Read budgets and reasons without reserving funds or settling old work."""
        if plan.get('payout_plan'):raise StateError('Связанный план эпохи обслуживается только через mina_pay.')
        import owner_income
        h=self.health();BatchWorkflow._identity(plan,h)
        store=self.legacy.store(readonly=True)
        self.legacy._unsettled(store)
        reserve=sum(max(0,n) for n in self.legacy.carry().values())
        income=owner_income.summary(store,plan['wallet'],plan['network'])
        funds=self.legacy._available_funds(store,h,reserve)
        own_reserved=0
        if digest and (self.state_dir/FILENAME).exists():
            batches=BatchStore(self.state_dir,create=False)
            for row in batches.rows(digest):
                if row['status'] not in ('confirmed','failed','cancelled'):
                    own_reserved+=plan['fee_nano']+sum(r['amount_nano'] for r in plan['batches'][row['index']]['recipients'])
        remaining=own_reserved if digest else sum(r['amount_nano'] for r in plan['recipients'])+plan['fee_nano']*len(plan['batches'])
        # A reviewed, immutable per-operation advance can use carry liquidity,
        # never other pending payments or more than the authorized total gap.
        # Carry liabilities themselves remain unchanged in ordinary accounting.
        advance=min(plan.get('reserve_advance_nano',0),reserve)
        wallet_available=funds['available_nano']+own_reserved
        available=max(0,min(income['available_nano']+own_reserved,wallet_available+advance))
        pending=[d for d,p in store.list_plans(plan['wallet'],plan['network']) if store.settlement(d) is None]
        h.update(batch_budget={'needed_nano':remaining,'available_nano':available,'shortfall_nano':max(0,remaining-available),
                              'carry_nano':reserve,'liquid_nano':h['account']['liquid_nano'],
                              'reserve_advance_nano':advance,
                              'projected_carry_shortfall_nano':max(0,remaining-wallet_available),
                              'owner_income_nano':income['available_nano']+own_reserved,'ordinary_unsettled':pending})
        return h

    def approve(self,plan,digest=None):
        import owner_income
        h=self.inspect(plan,digest);budget=h['batch_budget'];store=self.legacy.store(readonly=True)
        if budget['ordinary_unsettled']:raise StateError('Сначала завершите сверку текущих обычных выплат/вывода; batch их не изменяет.')
        if h['account']['nonce']!=h['account']['inferred_nonce'] or h['pooled_commands'] or h['pooled_zkapps']:
            raise StateError('В кошельке есть ожидающие транзакции; новый пакет не отправлен.')
        income=owner_income.summary(store,plan['wallet'],plan['network'])
        owner_income.check_external_spends(store,plan['wallet'],plan['network'],h,income)
        self.legacy._verify_previous(store,h,verify_batches=False)
        if budget['shortfall_nano']:
            from money import nano_to_mina
            raise StateError('Для batch не хватает {:.9f} MINA сверх защищённых резервов.'.format(nano_to_mina(budget['shortfall_nano'])))
        return h

    def build(self,expected):
        import batch_signer
        return batch_signer.build(self.config,expected)

    def sign(self,unsigned,expected,password):
        import batch_signer
        return batch_signer.sign(self.config,unsigned,expected,password)

    def validate(self,signed,expected):
        import batch_signer
        return batch_signer.validate(self.config,signed,expected)

    def submit(self,signed,expected):
        packet={**expected,'network':'mainnet','fee_nano':str(expected['fee_nano']),
                'recipients':[{'recipient':r['recipient'],'amount_nano':str(r['amount_nano'])} for r in expected['recipients']]}
        # Do not pass archive-only fields to the strict signer schema.
        packet={k:packet[k] for k in ('network','wallet','nonce','fee_nano','memo','recipients')}
        result=self.validate(signed,packet)
        if result['tx_hash']!=expected['tx_hash']:raise StateError('Prepared hash changed')
        return submit_once(self.config,result['query'],expected['tx_hash'])

    def proof(self,expected,required_depth=3):
        import batch_archive
        import preflight
        for attempt in range(3):
            try:
                result=batch_archive.verify(self.config,expected,required_depth=required_depth)
                break
            except preflight.PreflightArchiveLagError:
                if attempt==2:raise
                print('Архив догоняет свежий блок; повторяю только сверку batch…',flush=True)
                time.sleep(15)
        if result['status']=='unknown':
            data=preflight._graphql(self.config,'query($pk:PublicKey!){pooledZkappCommands(publicKey:$pk){hash}}',{'pk':expected['wallet']})
            if any(p['hash']==expected['tx_hash'] for p in data['pooledZkappCommands']):return {'status':'pending','in_pool':True,'tx_hash':expected['tx_hash']}
        return result


def show_plan(plan,budget=None):
    from payout_display import table,mina,account_link,paint
    rows=[]
    for batch in plan['batches']:
        for r in batch['recipients']:rows.append([batch['index']+1,account_link(r['recipient']),mina(r['amount_nano'])])
    print(paint('Mainnet · настоящий batch pay','cyan'))
    print('Отправитель: '+account_link(plan['wallet']))
    print(table(rows,headers=['Пакет','Адрес получателя','Сумма MINA'],colalign=('right','left','right')))
    total=sum(r['amount_nano'] for r in plan['recipients']);fees=plan['fee_nano']*len(plan['batches'])
    print('Memo: '+plan['memo'])
    print('Получателей: {} · пакетов: {} · размер пакета: {}'.format(len(plan['recipients']),len(plan['batches']),plan['batch_size']))
    print('Получателям: {} MINA · комиссия за пакет: {} MINA'.format(mina(total),mina(plan['fee_nano'])))
    print('Комиссии всего: {} MINA · всего с кошелька: {} MINA'.format(mina(fees),mina(total+fees)))
    if plan.get('reserve_advance_nano'):
        print(paint('Разрешено временно использовать резерв: до {} MINA только для этого плана. Обязательства делегаторам остаются полностью.'.format(mina(plan['reserve_advance_nano'])),'yellow'))
    if budget is not None:
        print(table([[name,mina(budget[key])] for name,key in (
            ('Баланс кошелька','liquid_nano'),('Защищённый резерв делегаторов','carry_nano'),
            ('Доступно с разрешённым использованием резерва' if plan.get('reserve_advance_nano') else 'Доступно для batch после всех резервов','available_nano'),('Недостаёт','shortfall_nano'))],headers=['Проверка средств','MINA'],colalign=('left','right')))
        if budget['ordinary_unsettled']:print(paint('Текущая обычная операция ещё не сверена. Завершите её исходной командой; batch её не меняет.','yellow'))
        if plan.get('reserve_advance_nano'):
            print(paint('Для восстановления покрытия обязательств после пакетов потребуется: {} MINA.'.format(mina(budget['projected_carry_shortfall_nano'])),'yellow'))


def show_status(store,digest,observed=()):
    from payout_display import paint
    proofs={p['index']:p for p in observed}
    names={'planned':'Не отправлен','prepared':'Подписан; отправка не начата','unknown':'Исход пока неизвестен','confirmed':'Подтверждён','failed':'Не выполнен в цепочке','cancelled':'Не подписан; отменён после сбоя предыдущего пакета'}
    for row in store.rows(digest):
        proof=proofs.get(row['index']) or row['proof'] or {}
        name='В пуле' if proof.get('in_pool') else 'Applied' if proof.get('in_chain_status')=='applied' else names[row['status']]
        print(paint('Пакет {}: {} · nonce={} · подтверждения {}/3'.format(row['index']+1,name,row['nonce'] if row['nonce'] is not None else '—',proof.get('depth','—')),
                    'green' if row['status']=='confirmed' else 'red' if row['status']=='failed' else 'yellow'))
        if row['tx_hash']:print('  https://minascan.io/mainnet/tx/'+row['tx_hash']+'?type=zk-tx')


def _parser():
    parser=argparse.ArgumentParser(description='Mainnet zkApp batch: отдельный журнал, подпись, архивная сверка; обычные планы не изменяются')
    commands=parser.add_subparsers(dest='command',required=True)
    for command in ('preview','send','resume','status'):
        sub=commands.add_parser(command)
        sub.add_argument('--config');sub.add_argument('--plan')
        if command in ('preview','send'):
            sub.add_argument('--proposal',type=Path,help='готовый JSON-план из preview')
            sub.add_argument('--source-plan',help='неизменяемый обычный план: взять только адреса, не изменять его')
            sub.add_argument('--each-nano',type=int,default=1)
            sub.add_argument('--fee-nano',type=int)
            sub.add_argument('--batch-size',type=int,default=30)
            sub.add_argument('--memo',default='batch_test')
            sub.add_argument('--operation',default='batch_test')
            sub.add_argument('--reserve-advance-nano',type=int,help='явное разрешение временно использовать резерв в пределах этой суммы; только новый план из --source-plan')
        if command in ('send','resume'):sub.add_argument('--wait-seconds',type=int,default=900)
    return parser


def read_password():
    # getpass otherwise falls back to visible stdin when there is no terminal.
    with warnings.catch_warnings():
        warnings.simplefilter('error',getpass.GetPassWarning)
        try:
            return getpass.getpass('Пароль зашифрованного кошелька (скрытый ввод): ')
        except getpass.GetPassWarning:
            raise StateError('Для скрытого ввода пароля запустите send в интерактивном терминале.') from None


def _proposal(args,adapter):
    if args.proposal and args.source_plan:raise StateError('Укажите либо --proposal, либо --source-plan')
    if args.proposal:
        if args.reserve_advance_nano is not None:
            raise StateError('Разрешение на резерв должно быть сохранено при preview из --source-plan; готовое предложение не изменяется.')
        if args.proposal.is_symlink():raise StateError('Proposal must be a regular file')
        plan=canonical_plan(json.loads(args.proposal.read_text()))
    else:
        if not args.source_plan:raise StateError('Укажите --proposal или --source-plan')
        source=adapter.legacy.store(readonly=True).get_plan(args.source_plan)
        if source.get('kind','payout')!='payout' or (source['wallet'],source['network'])!=(adapter.config['SEND_FROM_ADDRESS'],adapter.config['EXPECTED_CHAIN_ID']):
            raise StateError('Исходный план принадлежит другому кошельку/сети или не является выплатой')
        health=adapter.health()
        plan=canonical_plan({'wallet':source['wallet'],'network':source['network'],'era':str(health['era']),
            'operation':args.operation,'memo':args.memo,'fee_nano':args.fee_nano if args.fee_nano is not None else adapter.config['DEFAULT_TX_FEE'],
            'batch_size':args.batch_size,'recipients':[{'recipient':r['recipient'],'amount_nano':args.each_nano} for r in source['payments']]})
        if args.reserve_advance_nano is not None:
            plan=canonical_plan(dict(plan,reserve_advance_nano=args.reserve_advance_nano))
    if (plan['wallet'],plan['network'])!=(adapter.config['SEND_FROM_ADDRESS'],adapter.config['EXPECTED_CHAIN_ID']):
        raise StateError('Предложение не соответствует настроенному mainnet-кошельку')
    return plan


def main(argv=None):
    args=_parser().parse_args(argv)
    if args.plan and getattr(args,'reserve_advance_nano',None) is not None:
        raise StateError('Лимит использования резерва сохранён в неизменяемом плане; изменить его при продолжении нельзя.')
    from tunnel import load_config,ensure
    from payout_display import paint,mina
    config=load_config(args.config)
    state=PROJECT/'.payout-state'
    if args.command=='status':
        if not (state/FILENAME).exists():print('Пакетных операций ещё нет.');return 0
        store=BatchStore(state,create=False)
        if args.plan:show_plan(store.get_plan(args.plan));show_status(store,args.plan)
        else:
            for digest,plan in store.list_plans(config['SEND_FROM_ADDRESS'],config['EXPECTED_CHAIN_ID']):print(plan['operation']+': '+digest)
        return 0
    ensure(config)
    adapter=MainnetAdapter(config)
    if args.command=='preview':
        plan=_proposal(args,adapter);health=adapter.inspect(plan)
        show_plan(plan,health['batch_budget'])
        for batch in plan['batches']:
            adapter.build(signer_packet(plan,batch['index'],health['account']['nonce']+batch['index']))
        destination=state/'batch-proposals'/(plan['operation']+'.json')
        destination.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if destination.is_symlink():raise StateError('Proposal destination must not be a symbolic link')
        if destination.exists() and canonical_plan(json.loads(destination.read_text()))!=plan:
            raise StateError('Существующее предложение отличается; выберите другое имя операции')
        if not destination.exists():atomic_write_json(destination,plan)
        print('Неподписанный пакет проверен; отправки нет. Предложение: '+str(destination.relative_to(PROJECT)))
        return 0
    if not 0<=args.wait_seconds<=86400:raise StateError('--wait-seconds must be 0..86400')
    digest=args.plan
    with wallet_lock(state,config['SEND_FROM_ADDRESS']):
        if digest:
            store=BatchStore(state,create=False);plan=store.get_plan(digest)
            if plan.get('payout_plan'):raise StateError('Для этой пакетной эпохи используйте mina_pay с исходным планом эпохи.')
            if (plan['wallet'],plan['network'])!=(config['SEND_FROM_ADDRESS'],config['EXPECTED_CHAIN_ID']):raise StateError('Batch belongs to another wallet/network')
        elif args.command=='resume':raise StateError('Для сверки укажите --plan')
        else:
            plan=_proposal(args,adapter)
            health=adapter.inspect(plan);show_plan(plan,health['batch_budget'])
            adapter.approve(plan)
            # All packets are built/limit-checked before a durable reservation,
            # so an invalid later packet cannot strand an accepted operation.
            for batch in plan['batches']:
                adapter.build(signer_packet(plan,batch['index'],health['account']['nonce']+batch['index']))
            if input('Подтвердить отправку показанного mainnet batch? [y/N]: ').strip().lower()!='y':
                print('Отправка отменена; план не зарезервирован.');return 0
            store=BatchStore(state);digest=store.create_plan(plan)
        engine=BatchWorkflow(store,adapter)
        if args.plan:show_plan(plan)
        if args.command=='send' and args.plan and any(r['status'] in ('planned','prepared') for r in store.rows(digest)):
            if input('Продолжить отправку оставшихся пакетов этого плана? [y/N]: ').strip().lower()!='y':return 0
        print('Batch plan: '+digest,flush=True)
        print('Продолжить сверку: python3 batch_mainnet.py resume --plan '+digest,flush=True)
        password=None
        def password_provider():
            nonlocal password
            if password is None:password=read_password()
            return password
        try:
            if args.command=='send':engine.send_next(digest,password_provider)
            deadline=time.monotonic()+args.wait_seconds;previous=None
            while True:
                observations=engine.reconcile(digest)
                rows=store.rows(digest);view=[(r['status'],r['tx_hash']) for r in rows],observations
                if view!=previous:show_status(store,digest,observations);previous=view
                if all(r['status']=='confirmed' for r in rows):
                    snapshot=store.snapshot(plan['wallet'],plan['network'])
                    print(paint('Batch подтверждён, отдельный учёт обновлён.','green'))
                    print('Batch-расходы с комиссиями: {} MINA · резерв batch: {} MINA'.format(mina(snapshot['spent_nano']),mina(snapshot['reserved_nano'])))
                    if plan.get('reserve_advance_nano'):
                        try:
                            from payout_display import table
                            budget=adapter.inspect(plan,digest)['batch_budget']
                            print(table([['Баланс кошелька',mina(budget['liquid_nano'])],
                                         ['Обязательства перед делегаторами',mina(budget['carry_nano'])],
                                         ['Нужно пополнить для покрытия',mina(budget['projected_carry_shortfall_nano'])]],
                                        headers=['После сверки','MINA'],colalign=('left','right')))
                        except Exception:
                            print('Учёт сохранён. Свежий баланс временно недоступен; повторите resume для итоговой таблицы.')
                    return 0
                if any(r['status']=='failed' for r in rows):raise StateError('Пакет не выполнен; учтена только комиссия. Автоматического повтора нет.')
                if args.command=='send' and not any(r['status']=='unknown' for r in rows):
                    engine.send_next(digest,password_provider)
                    continue
                if args.command=='resume' and not any(r['status']=='unknown' for r in rows):
                    print('Для оставшихся неотправленных пакетов нужен send с подтверждением.');return 2
                if time.monotonic()>=deadline:print('Ожидание закончено; продолжите сверку сохранённого плана.');return 2
                time.sleep(min(15,max(0,deadline-time.monotonic())))
        finally:password=None


if __name__=='__main__':
    try:raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nОстановлено. Сохранённые пакеты не сбрасываются; продолжите resume.');raise SystemExit(130)
    except Exception as error:
        from payout_display import paint
        print(paint('Batch остановлен: '+str(error),'red'))
        print('Если пакет уже был подписан/отправлен, используйте напечатанный plan ID и resume. Не создавайте заменяющий план.')
        raise SystemExit(1)
