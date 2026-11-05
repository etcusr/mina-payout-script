"""Read-only health and local payout journal diagnostics."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

import preflight
import tunnel
from tunnel import load_config
from payout_display import paint, income_totals


def status(state_dir):
    """Inspect without creating a journal or changing legacy accounting."""
    root = Path(state_dir)
    database = root/'journal.sqlite3'
    if not database.exists():
        return {'journal': 'not_initialized', 'intents': [],
                'legacy_ledger_present': (root.parent/'payout_ledger.json').is_file()}
    if database.is_symlink(): raise ValueError('Journal must not be a symbolic link')
    connection = sqlite3.connect(database.resolve().as_uri()+'?mode=ro', uri=True)
    try:
        if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Journal integrity check failed')
        rows = connection.execute('SELECT status,COUNT(*) FROM intents GROUP BY status').fetchall()
        return {'journal': 'present', 'intents': {name: count for name, count in rows}}
    finally: connection.close()


def _parser():
    parser=argparse.ArgumentParser(description='Exact, recoverable Mina payouts; no blind retries')
    commands=parser.add_subparsers(dest='command',required=True)
    for name in ('doctor','status','init','plan','send','reconcile','withdraw','backup'):
        p=commands.add_parser(name)
        p.add_argument('--config')
        p.add_argument('--project-root',default=str(Path(__file__).resolve().parent))
        p.add_argument('--state-dir')
        if name in ('doctor','status','plan','send','reconcile'): p.add_argument('-e','--epoch',type=int)
        if name=='doctor':
            p.add_argument('--wallet',action='store_true'); p.add_argument('--print-epoch',action='store_true')
        if name=='status': p.add_argument('--online',action='store_true'); p.add_argument('--plan')
        if name=='init': p.add_argument('--import-legacy',action='store_true')
        if name=='plan':
            p.add_argument('--preview',action='store_true'); p.add_argument('--vrf',action='store_true'); p.add_argument('--vrf-refresh',action='store_true')
            p.add_argument('--json',action='store_true')
        if name in ('send','reconcile'): p.add_argument('--plan')
        if name in ('send','withdraw'):
            p.add_argument('--dry-run',action='store_true'); p.add_argument('--yes',action='store_true')
            p.add_argument('--complete',action='store_true',help='automatic backups, confirmation wait and verified accounting settlement')
            p.add_argument('--wait-seconds',type=int,help='bounded read-only confirmation wait; default configured timer')
        if name=='send': p.add_argument('--resume',action='store_true',help='resume the same immutable journal plan; never resubmit uncertain intents')
        if name=='reconcile':
            p.add_argument('--commit',action='store_true'); p.add_argument('--from-csv',action='store_true')
            p.add_argument('--force',action='store_true',help='unsupported: existing settled history is immutable')
            p.add_argument('--accept-pending',action='store_true',help='unsupported: absence cannot prove failure')
            p.add_argument('--wait',nargs='?',type=int,const=30,default=0,metavar='MINUTES')
        if name=='withdraw':
            p.add_argument('--amount',help='exact MINA decimal string; omitted: recorded unpaid owner income minus withdrawal fee')
            p.add_argument('--to'); p.add_argument('--operation-id',help='reuse the printed UUID for a withdrawal resume')
            p.add_argument('--new-operation',action='store_true',help='explicitly start another withdrawal, including the same amount previously settled')
        if name=='backup': p.add_argument('--destination',required=True)
    p=commands.add_parser('restore'); p.add_argument('--backup',required=True); p.add_argument('--destination',required=True)
    return parser


def _confirmation(yes, *, show_plan=True, required_depth=None):
    import getpass
    from money import nano_to_mina
    from payout_state import StateError
    def confirm(plan):
        if show_plan:
            from payout_display import PayoutDisplay
            PayoutDisplay().plan(plan,required_depth=required_depth)
        print(paint('Подтверждение сохранённого плана выплат','cyan'))
        if plan.get('operation_id'): print('Withdrawal operation ID: '+plan['operation_id'])
        if not yes and input('Подтвердить отправку оставшихся платежей? [y/N]: ').strip().lower()!='y': raise StateError('Cancelled before wallet unlock')
        return getpass.getpass('Пароль кошелька: ')
    return confirm


def wait_for_proofs(workflow,digest,seconds):
    import time
    from payout_workflow import same_payment
    seconds=max(0,int(seconds)); deadline=time.monotonic()+seconds
    plan=workflow.store().get_plan(digest)
    while True:
        result=workflow.reconcile(digest,commit=False)
        intents=result['intents']; proofs=result['chain_matches']
        ready=len(intents)==len(plan['payments']) and all(any(same_payment(i,p) and p['depth']>=workflow.tx_depth(plan) for p in proofs) for i in intents)
        if ready or time.monotonic()>=deadline:
            return {'plan':digest,'confirmed_on_fresh_chain':ready,'next':'reconcile --commit' if ready else 'pending; resume/status later, never repay'}
        print('Waiting for successful best-chain payment depth; journal remains resumable.',flush=True)
        time.sleep(min(15,max(0,deadline-time.monotonic())))


def automatic_backup(workflow, label):
    """Consistent private snapshot; a failed pre-send backup prevents signing."""
    import os
    import uuid
    from payout_state import wallet_lock
    with wallet_lock(workflow.state_dir,workflow.wallet):
        store=workflow.store()
        parent=workflow.state_dir/'backups'
        parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        extras=list(workflow.root.glob('*.csv'))
        for name in ('config.yml','payout_ledger.json'):
            if (workflow.root/name).exists(): extras.append(workflow.root/name)
        destination=parent/('auto-'+label+'-'+uuid.uuid4().hex[:12])
        store.backup(destination,extra_files=extras)
        return os.path.relpath(destination,workflow.root)


def complete_accounting(workflow, digest, seconds, resume_command):
    """Poll fresh guarded reconciliation; a timeout is pending, never success."""
    import time
    from payout_workflow import PendingPayments
    deadline=time.monotonic()+max(0,int(seconds))
    disconnected=False
    while True:
        try:
            settlement=workflow.reconcile(digest,commit=True)
        except preflight.PreflightConnectionError:
            disconnected=True
            remaining=deadline-time.monotonic()
            print(paint('Связь с нодой недоступна. Подтверждение пока проверить нельзя; отправленный перевод остаётся в журнале.','yellow'),flush=True)
            if remaining<=0:
                return {'plan':digest,'settled':False,'resume':resume_command,'pending_reason':'connectivity'}
            print(paint('Проверяю туннель. Повторяется только сверка, без повторной отправки.','yellow'),flush=True)
            try:
                tunnel.ensure(workflow.config)
            except tunnel.TunnelError:
                print(paint('Туннель пока недоступен; повторю проверку связи в пределах времени ожидания.','yellow'),flush=True)
            time.sleep(min(15,max(0,deadline-time.monotonic())))
        except PendingPayments:
            if disconnected:
                print(paint('Связь восстановлена, свежие проверки пройдены. Ожидаю подтверждения перевода.','green'),flush=True)
                disconnected=False
            remaining=deadline-time.monotonic()
            if remaining<=0:
                return {'plan':digest,'settled':False,'resume':resume_command}
            if workflow.observer is None:
                print('Ожидаю подтверждения в цепочке; после них учёт завершится автоматически.',flush=True)
            time.sleep(min(15,remaining))
        else:
            if disconnected:
                print(paint('Связь восстановлена, подтверждение проверено по свежей цепочке.','green'),flush=True)
            # Completion is only reported after a post-settlement backup too.
            backup=automatic_backup(workflow,'after-'+digest[:12])
            import owner_income
            from payout_state import wallet_lock
            with wallet_lock(workflow.state_dir,workflow.wallet):
                store=workflow.store()
                intents=store.list_intents(plan_digest=digest)
                kind=store.get_plan(digest).get('kind','payout')
                income=owner_income.summary(store,workflow.wallet,workflow.network)
            return dict(settlement,settled=True,backup=backup,kind=kind,
                        owner_income=income if income['baseline_nonce'] is not None else None,
                        confirmed=sum(i['status']=='confirmed' for i in intents),
                        failed=sum(i['status']=='failed' for i in intents))


def completion_report(result):
    if result.get('nothing_to_withdraw'):
        print(paint('Новых средств для вывода нет: учтённый доход уже выведен или зарезервирован.','green'))
        if result.get('owner_income') is not None: income_totals(result['owner_income'])
        return
    if result['settled']:
        print(paint('Сверка завершена, учёт сохранён. Подтверждено платежей: '+str(result['confirmed']),'yellow' if result['failed'] else 'green'))
        if result['failed']:
            if result['kind']=='withdrawal':
                print('Вывод не состоялся. Для новой попытки после этой сверки используйте --new-operation.')
            else:
                print('Неоплаченных платежей: '+str(result['failed'])+'. Их суммы сохранены в переносимых обязательствах.')
        if result.get('already_settled'):
            print('Эта операция уже была завершена; повторной отправки не было.')
            if result['kind']=='withdrawal' and not result['failed']:
                print('Для ещё одного вывода такой же суммы явно добавьте --new-operation.')
        if result.get('owner_income') is not None: income_totals(result['owner_income'])
        print('Резервная копия (от корня проекта): '+result['backup'])
    else:
        message=('Операция ещё не завершена: связь недоступна, текущие подтверждения неизвестны. Учёт не закрыт.'
                 if result.get('pending_reason')=='connectivity' else
                 'Операция ещё не завершена: подтверждений недостаточно. Учёт не закрыт.')
        print(paint(message,'yellow'))
        print('Для продолжения повторите: '+result['resume'])
    print('План: '+result['plan'])


def continuation_command(args, workflow, words):
    """Aliases enter this checkout; retain custom context with relative paths."""
    import os
    import shlex
    base=Path(__file__).resolve().parent
    words=list(words)
    if workflow.root!=base:
        words+=['--project-root',os.path.relpath(workflow.root,base)]
    if args.config:
        words+=['--config',os.path.relpath(Path(args.config).resolve(),base)]
    if args.state_dir:
        words+=['--state-dir',os.path.relpath(workflow.state_dir,base)]
    return shlex.join(words)


def main(argv=None):
    args=_parser().parse_args(argv)
    exit_code=0
    resume_hint=None
    display=None
    from payout_state import StateError, verified_restore, wallet_lock
    from payout_workflow import Workflow
    from payout_app import calculate_epoch, legacy_status, report, vrf_report
    from money import mina_to_nano, nano_to_mina
    try:
        if args.command=='restore':
            result={'restored_to':str(verified_restore(args.backup,args.destination))}
        elif args.command=='status' and args.epoch is None and not args.online and not args.plan:
            result=status(args.state_dir or str(Path(args.project_root)/'.payout-state'))
        elif args.command=='reconcile' and (args.force or args.accept_pending or (args.from_csv and args.commit)):
            raise StateError('Forced/pending/CSV financial commits are forbidden; use exact successful chain proofs')
        else:
            config=load_config(args.config or (str(Path(args.project_root)/'config.yml') if args.project_root!=str(Path(__file__).resolve().parent) else None))
            if args.command=='doctor':
                tunnel.ensure(config)
                result=preflight.check(config,epoch=args.epoch,require_wallet=args.wallet)
                if args.print_epoch: print(result['current_epoch']); return 0
                result={k:v for k,v in result.items() if k not in ('best_chain','pooled_commands')}
            else:
                workflow=Workflow(config,args.project_root,state_dir=args.state_dir)
                if (args.command in ('send','withdraw') and args.complete and not args.dry_run) or (args.command=='plan' and not args.json):
                    from payout_display import PayoutDisplay
                    display=PayoutDisplay()
                    workflow.observer=display

                def ensure_tunnel():
                    if display: display.line('Туннель: проверяю доступность сервисов…','cyan')
                    tunnel.ensure(config)
                    if display: display.line('Туннель: OK — сервисы доступны','green')
                if args.command=='init': result=workflow.initialize(import_legacy=args.import_legacy)
                elif args.command=='backup':
                    with wallet_lock(workflow.state_dir,workflow.wallet):
                        extras=list(workflow.root.glob('*.csv'))
                        for name in ('config.yml','payout_ledger.json'):
                            if (workflow.root/name).exists(): extras.append(workflow.root/name)
                        result={'backup':str(workflow.store().backup(args.destination,extra_files=extras))}
                elif args.command=='status':
                    if args.online: ensure_tunnel()
                    if args.epoch is not None and workflow._legacy_epoch(args.epoch,config.get('LEGACY_PROTOCOL_VERSION_ID')):
                        result=legacy_status(workflow,args.epoch,online=args.online)
                    elif args.online:
                        digest=args.plan or workflow.plan_for_epoch(args.epoch)
                        result=workflow.reconcile(digest,commit=False)
                    else:
                        store=workflow.store()
                        result={'plans':[{'digest':d,'epoch':p['epoch'],'era':p['era'],'kind':p.get('kind','payout'),'operation_id':p.get('operation_id'),'settled':store.settlement(d) is not None} for d,p in store.list_plans(workflow.wallet,workflow.network) if args.epoch is None or p['epoch']==args.epoch],
                                'intents':store.list_intents(workflow.wallet,network=workflow.network),'carry_nano':workflow.carry()}
                elif args.command=='plan':
                    if args.epoch is None: raise StateError('--epoch is required')
                    # Legacy local report is safe even when the network is unavailable.
                    ensure_tunnel()
                    result=calculate_epoch(workflow,args.epoch,preview=args.preview)
                    if not args.json: report(result)
                    if args.vrf or args.vrf_refresh:
                        ensure_tunnel()
                        if args.json:
                            import contextlib
                            with contextlib.redirect_stdout(sys.stderr):
                                result['vrf']=vrf_report(workflow,args.epoch,refresh=args.vrf_refresh)
                        else:
                            print(json.dumps({'vrf':vrf_report(workflow,args.epoch,refresh=args.vrf_refresh)},default=str,sort_keys=True))
                    if not args.json: return 0
                elif args.command=='send':
                    digest=args.plan or workflow.plan_for_epoch(args.epoch)
                    ensure_tunnel()
                    if args.complete and not args.dry_run:
                        plan=workflow.store().get_plan(digest)
                        if plan.get('kind','payout')!='payout': raise StateError('send requires a payout plan')
                        resume_hint=continuation_command(args,workflow,['mina_pay','--plan',digest])
                        if display: display.plan(plan,workflow.store().list_intents(plan_digest=digest),required_depth=workflow.tx_depth(plan))
                        automatic_backup(workflow,'before-'+digest[:12])
                        if display: display.line('Резервная копия перед отправкой: OK','green')
                        if len(workflow.store().list_intents(plan_digest=digest))<len(plan['payments']):
                            workflow.send(digest,confirm=_confirmation(args.yes,show_plan=display is None,required_depth=workflow.tx_depth(plan)))
                        resume=continuation_command(args,workflow,['mina_pay','--plan',digest] if args.plan else ['mina_pay','--epoch',str(args.epoch)])
                        seconds=args.wait_seconds if args.wait_seconds is not None else config.get('AUTO_SETTLE_WAIT_SECONDS',3600)
                        if display: display.line('\nПодтверждения и сверка — проверяю свежую цепочку…','cyan')
                        result=complete_accounting(workflow,digest,seconds,resume)
                        exit_code=0 if result['settled'] else 2
                    else:
                        result=workflow.send(digest,dry_run=args.dry_run,confirm=_confirmation(args.yes,show_plan=display is None,required_depth=workflow.tx_depth(workflow.store().get_plan(digest))))
                    if not args.dry_run and not args.complete:
                        print(json.dumps(result,default=str,sort_keys=True))
                        result=wait_for_proofs(workflow,digest,args.wait_seconds if args.wait_seconds is not None else config.get('TX_CHECK_TIMER_SECONDS',0))
                elif args.command=='reconcile':
                    legacy=args.epoch is not None and workflow._legacy_epoch(args.epoch,config.get('LEGACY_PROTOCOL_VERSION_ID'))
                    if legacy:
                        if args.commit: raise StateError('Legacy history is read-only; no automatic re-commit or undo')
                        if not args.from_csv: ensure_tunnel()
                        result=legacy_status(workflow,args.epoch,online=not args.from_csv)
                    else:
                        if args.from_csv: raise StateError('CSV preview only applies to legacy sent records')
                        digest=args.plan or workflow.plan_for_epoch(args.epoch)
                        ensure_tunnel()
                        if args.wait: wait_for_proofs(workflow,digest,args.wait*60)
                        result=workflow.reconcile(digest,commit=args.commit)
                elif args.command=='withdraw':
                    ensure_tunnel()
                    amount=mina_to_nano(args.amount) if args.amount is not None else None
                    recipient=args.to or config.get('WITHDRAW_TO_ADDRESS')
                    if args.complete:
                        fallback=['mina_withdraw'] if amount is None else ['mina_withdraw','--amount',format(nano_to_mina(amount),'.9f')]
                        if recipient: fallback+=['--to',recipient]
                        if args.operation_id: fallback+=['--operation-id',args.operation_id]
                        # Never preserve --new-operation in retry guidance.
                        resume_hint=continuation_command(args,workflow,fallback)

                    def withdrawal_confirmation(plan):
                        nonlocal resume_hint
                        payment=plan['payments'][0]
                        if args.complete:
                            resume_hint=continuation_command(args,workflow,['mina_withdraw','--amount',format(nano_to_mina(payment['amount_nano']),'.9f'),'--to',payment['recipient'],'--operation-id',plan['operation_id']])
                        if display: display.plan(plan,required_depth=workflow.tx_depth(plan))
                        return _confirmation(args.yes,show_plan=display is None,required_depth=workflow.tx_depth(plan))(plan)

                    if args.complete and not args.dry_run: automatic_backup(workflow,'before-withdrawal')
                    result=workflow.withdraw(recipient,amount,operation_id=args.operation_id,dry_run=args.dry_run,confirm=withdrawal_confirmation,reuse_existing=args.complete,new_operation=args.new_operation)
                    if args.complete and not args.dry_run and not result.get('nothing_to_withdraw'):
                        plan=workflow.store().get_plan(result['plan'])
                        payment=plan['payments'][0]
                        resume_hint=continuation_command(args,workflow,['mina_withdraw','--amount',format(nano_to_mina(payment['amount_nano']),'.9f'),'--to',payment['recipient'],'--operation-id',plan['operation_id']])
                        seconds=args.wait_seconds if args.wait_seconds is not None else config.get('AUTO_SETTLE_WAIT_SECONDS',3600)
                        result=complete_accounting(workflow,result['plan'],seconds,resume_hint)
                        exit_code=0 if result['settled'] else 2
                    elif not args.dry_run and not result.get('nothing_to_withdraw'):
                        print(json.dumps(result,default=str,sort_keys=True))
                        result=wait_for_proofs(workflow,result['plan'],args.wait_seconds if args.wait_seconds is not None else config.get('TX_CHECK_TIMER_SECONDS',0))
        if getattr(args,'complete',False) and not args.dry_run:
            completion_report(result)
        else:
            print(json.dumps(result,sort_keys=True,default=str))
        return exit_code
    except KeyboardInterrupt:
        print('Прервано. Состояние сохранено. При продолжении вывода не используйте --new-operation.',file=sys.stderr)
        if resume_hint: print('Для продолжения: '+resume_hint,file=sys.stderr)
        return 130
    except (preflight.PreflightError,tunnel.TunnelError,StateError) as error:
        if display: print(paint('Проверка/операция остановлена. Успешное завершение не подтверждено.','red',stream=sys.stderr),file=sys.stderr)
        print(paint(str(error),'red',stream=sys.stderr),file=sys.stderr)
        if resume_hint: print('Для продолжения: '+resume_hint,file=sys.stderr)
        return 1
    except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
        print(paint('Операция остановлена: проверьте настройки, входные суммы, сервисы и журнал. Не повторяйте перевод вручную.','red',stream=sys.stderr),file=sys.stderr)
        if resume_hint: print('Для продолжения: '+resume_hint,file=sys.stderr)
        return 1
    except Exception:
        # Transport/driver errors can contain DSNs; never print raw exceptions.
        print(paint('Ошибка сервиса или учёта: операция остановлена. Требуется проверка состояния перед продолжением.','red',stream=sys.stderr),file=sys.stderr)
        if resume_hint: print('Для продолжения: '+resume_hint,file=sys.stderr)
        return 1


if __name__=='__main__': raise SystemExit(main())
