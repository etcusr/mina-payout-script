"""Human progress from verified workflow events; no network or state writes."""
import os
import shutil
import sys
import time
from money import nano_to_mina


def mina(value):
    return format(nano_to_mina(value), '.9f')


def paint(text, tone, *, stream=None):
    stream = stream if stream is not None else sys.stdout
    colors = {'green':'32', 'yellow':'33', 'red':'31', 'cyan':'36'}
    enabled = not os.environ.get('NO_COLOR') and (
        os.environ.get('CLICOLOR_FORCE') == '1' or
        (stream.isatty() and os.environ.get('TERM') != 'dumb'))
    return f'\033[{colors[tone]}m{text}\033[0m' if enabled and tone in colors else text


def account_link(address):
    """OSC 8 retains the full visible key; pipes/logs remain plain text."""
    if sys.stdout.isatty() and os.environ.get('TERM') != 'dumb' and not os.environ.get('NO_HYPERLINK'):
        return '\033]8;;https://minascan.io/mainnet/account/'+address+'\033\\'+address+'\033]8;;\033\\'
    return address


def status_tone(status):
    return 'green' if status == 'confirmed' else 'red' if status in ('failed','unknown','reserved') else 'yellow'


def table(rows, headers, **kwargs):
    """Keep exact numeric strings and full OSC-8 addresses inside visible borders."""
    from tabulate import tabulate
    return tabulate(rows,headers=headers,tablefmt='rounded_grid',disable_numparse=True,**kwargs)


def payment_numbers(numbers):
    runs=[]
    for number in numbers:
        if runs and number==runs[-1][-1]+1: runs[-1].append(number)
        else: runs.append([number])
    return ', '.join(str(r[0]) if len(r)==1 else f'{r[0]}–{r[-1]}' for r in runs)


def income_totals(summary):
    """Render an accounting snapshot supplied after settlement, without writes."""
    print(paint('\nИтоговый учёт · данные БД','cyan'))
    rows=[]
    for label,key in (
        ('Выведено (с комиссиями)','withdrawn_with_fees_nano'),
        ('Осталось (до комиссии вывода)','available_nano'),
        ('Зарезервировано (с комиссиями)','reserved_with_fees_nano'),
    ):
        value=summary[key]
        tone='red' if value<0 else 'yellow' if key=='reserved_with_fees_nano' and value else 'green'
        rows.append([label,paint(mina(value),tone)])
    print(table(rows,headers=['Показатель','MINA'],colalign=('left','right')))


class PayoutDisplay:
    def __init__(self):
        self.last_rows = {}
        self.health_shown = False
        self.polling = False
        self.last_depths = {}
        self.proof_plan = None
        self.final_shown = False
        self.progress_active = False
        self.progress_length = 0
        self.progress_key = None
        self.progress_at = 0

    def finish(self):
        """Leave the cursor on a fresh line before messages, errors or prompts."""
        if self.progress_active:
            print(flush=True)
            self.progress_active = False
        self.progress_key = None

    def line(self, text, tone=None):
        self.finish()
        print(paint(text,tone), flush=True)

    def progress(self, text, tone, *, complete=False):
        stamp='['+time.strftime('%H:%M:%S')+'] '+text
        interactive=(sys.stdout.isatty() and os.environ.get('TERM')!='dumb'
                     and not os.environ.get('NO_COLOR'))
        columns=shutil.get_terminal_size().columns
        fits=len(stamp)<columns
        if self.progress_active and (not interactive or self.progress_length>=columns or not fits):
            # After resizing, a formerly single-line summary may occupy several
            # rows. Leave it as history; never erase only its final wrapped row.
            self.finish()
        now=time.monotonic()
        written=False
        if interactive and fits and not complete:
            sys.stdout.write(('\r\033[2K' if self.progress_active else '')+paint(stamp,tone))
            sys.stdout.flush(); self.progress_active=True
            self.progress_length=len(stamp)
            written=True
        elif text!=self.progress_key or now-self.progress_at>=60 or complete:
            self.finish()
            print(paint(stamp,tone),flush=True)
            written=True
        self.progress_key=text
        # Idle plain logs get a heartbeat, not a line on every polling tick.
        if written: self.progress_at=now

    def plan(self, plan, intents=None, *, required_depth=None):
        self.line('\nПлан выплат · эпоха '+str(plan['epoch']), 'cyan')
        statuses = {i['payment_index']: i['status'] for i in (intents or ())}
        labels = {'submitted': 'отправлен', 'confirmed': 'подтверждён',
                  'failed': 'не выполнен', 'unknown': 'исход неизвестен', 'reserved': 'зарезервирован'}
        rows=[]
        for j, p in enumerate(plan['payments']):
            status = labels.get(statuses.get(j), 'не отправлен' if intents is not None else 'статус не загружен')
            rows.append([j+1,account_link(p['recipient']),mina(p['amount_nano']),mina(p['fee_nano']),paint(status,status_tone(statuses.get(j)))])
        if rows:
            self.line(table(rows,['№','Адрес получателя','Сумма MINA','Сеть MINA','Статус журнала'],colalign=('right','left','right','right','left')))
        amount = sum(p['amount_nano'] for p in plan['payments'])
        fees = sum(p['fee_nano'] for p in plan['payments'])
        self.line(f"Итого: {len(plan['payments'])} платежей · {mina(amount)} MINA")
        self.line(f'Сетевые комиссии: {mina(fees)} MINA · Всего: {mina(amount+fees)} MINA')
        self.line('Applied + блоков после включения: '+str(plan['min_confirmations'] if required_depth is None else required_depth))

    def __call__(self, event, **data):
        if event == 'health_start':
            if not self.polling:
                self.line('\nПроверка валидатора и архива…','cyan')
        elif event == 'health_ok':
            if not self.polling or not self.health_shown:
                h = data['health']
                self.line('  Валидатор: OK — синхронизация, сеть и производитель проверены; высота '+str(h.get('tip_height', '—')),'green')
                self.line('  Архив: OK — проверены пропуски, связи блоков и совпадение с цепочкой валидатора','green')
                if h.get('canonical_lag_blocks') is not None:
                    self.line('  Отставание canonical от вершины: '+str(h['canonical_lag_blocks'])+' блоков (в допустимых пределах)')
                if h.get('epoch_closed'):
                    self.line('  Эпоха выплат: закрыта','green')
                self.health_shown = True
        elif event == 'funds_ok':
            f = data['funds']
            self.line(f"  Баланс: OK — доступно после резервов {mina(f['available_nano'])} MINA; осталось отправить с комиссиями {mina(f['remaining_nano'])} MINA",'green')
            self.line('  Резерв переносимых обязательств: '+mina(f['reserved_nano'])+' MINA')
        elif event == 'sending':
            p = data['payment']
            self.line(f"Отправка {data['index']+1}/{data['total']}: {account_link(p['recipient'])} · {mina(p['amount_nano'])} MINA · nonce={data['nonce']}",'cyan')
        elif event == 'submitted':
            self.line('  Принято нодой · tx='+data['tx_hash']+' · ожидает включения в цепочку','yellow')
        elif event == 'locked':
            self.line('Кошелёк: заблокирован','green')
        elif event == 'previous_start':
            self.line('Проверяю предыдущие платежи в свежей цепочке: '+str(data['count'])+'…','cyan')
        elif event == 'previous_ok':
            for required,group in sorted(data['groups'].items()):
                self.line(f"Предыдущие платежи: OK — {group['count']} Applied · подтверждения минимум {group['depth']}/{required}",'green')
        elif event == 'income':
            summary=data['summary']
            self.line('\nДоход владельца · учёт в БД','cyan')
            rows=[[r['epoch'],mina(r['validator_nano']),mina(r['retained_own_nano']),mina(r['payout_fees_nano'])] for r in summary['credits']]
            if rows:
                self.line(table(rows,['Эпоха','Комиссия MINA','Свои на кошельке MINA','Расходы на выплаты MINA'],colalign=('right',)*4))
            self.line('Доход после расходов: '+mina(summary['earned_nano'])+' MINA')
            self.line('Уже выплачено своим адресам как делегаторам: '+mina(sum(r['own_paid_nano'] for r in summary['credits']))+' MINA (повторно в вывод не включается)')
            self.line('Уже выведено с комиссиями: '+mina(summary['withdrawn_with_fees_nano'])+' MINA')
            self.line('Зарезервировано с комиссиями: '+mina(summary['reserved_with_fees_nano'])+' MINA')
            self.line('Получателю: '+mina(data['amount_nano'])+' MINA · комиссия вывода: '+mina(data['fee_nano'])+' MINA','cyan')
        elif event == 'proofs':
            self.proofs(**data)

    def proofs(self, plan, intents, chain, pool, required_depth=None):
        from payout_workflow import same_payment
        self.polling = True
        required_depth=plan['min_confirmations'] if required_depth is None else required_depth
        key=(plan.get('epoch'),plan.get('operation_id'),plan.get('input_fingerprint'),required_depth,
             tuple((p['recipient'],p['amount_nano'],p['fee_nano'],p.get('memo')) for p in plan['payments']))
        if key!=self.proof_plan:
            self.finish(); self.last_rows={}; self.last_depths={}; self.final_shown=False
            self.proof_plan=key
        first=not self.last_rows
        indexed = {i['payment_index']: i for i in intents}
        counts=dict(confirmed=0,included=0,pooled=0,failed=0,unknown=0,unsent=0)
        rows=[]; changes={}; regressions=[]; depths=[]; waiting_depths=[]
        for j, p in enumerate(plan['payments']):
            i = indexed.get(j)
            depth=None; block='—'; block_hash=None
            if i is None:
                phase='unsent'; label='Не отправлен'; tone='yellow'
            elif i['status'] == 'failed':
                phase='failed'; label='Не выполнен'; tone='red'
            else:
                matches = [r for r in chain if same_payment(i, r) and r.get('on_best_chain') is True and r.get('status') == 'applied'
                           and (not i.get('tx_hash') or r.get('tx_hash')==i['tx_hash'])]
                if len(matches) == 1:
                    depth = matches[0]['depth']
                    mature = depth >= required_depth
                    phase='confirmed' if mature else 'included'; tone='green' if mature else 'yellow'
                    label='Подтверждён' if mature else 'Applied'
                    block=matches[0].get('height','—'); block_hash=matches[0].get('block_hash')
                    depths.append(depth)
                    if not mature: waiting_depths.append(depth)
                elif any(same_payment(i, r) for r in pool):
                    phase='pooled'; label='В пуле'; tone='yellow'
                else:
                    phase='unknown'; label='Не найден'; tone='red'
            counts[phase]+=1
            state=(phase,block_hash,i.get('tx_hash') if i else None)
            if not first and self.last_rows.get(j)!=state:
                changes.setdefault((label,tone),[]).append(j+1)
            previous_depth=self.last_depths.get(j)
            if previous_depth is not None and depth is not None and depth<previous_depth:
                regressions.append(j+1)
            self.last_rows[j]=state; self.last_depths[j]=depth
            rows.append([j+1,account_link(p['recipient']),mina(p['amount_nano']),
                         i['nonce'] if i else '—',block,paint(label,tone),
                         paint(f'{depth if depth is not None else "—"}/{required_depth}',tone)])
        total = len(plan['payments'])
        complete=counts['confirmed']==total
        if first or complete and not self.final_shown:
            self.line('\nПодтверждения транзакций'+(' · итоговая проверка' if complete else ''),'cyan')
            if rows:
                self.line(table(rows,['№','Адрес получателя','Сумма MINA','Nonce','Блок','Статус','Подтв.'],
                                colalign=('right','left','right','right','right','left','right')))
        else:
            for (label,tone),numbers in changes.items():
                self.line(f'Платежи №{payment_numbers(numbers)}: {label}.',tone)
        if regressions:
            self.line('Глубина снизилась: платежи №'+payment_numbers(regressions)+'. Проверяю свежую цепочку.','yellow')
        if counts['unknown'] and (first or any(label=='Не найден' for label,_ in changes)):
            self.line('Не найден в цепочке/пуле: повторная отправка запрещена; продолжаю сверку.','red')
        if counts['failed'] and (first or any(label=='Не выполнен' for label,_ in changes)):
            self.line('Не выполнен: nonce занят другой подтверждённой транзакцией.','red')
        self.final_shown=complete
        relevant=waiting_depths or depths
        depth_text='—' if not relevant else str(min(relevant)) if min(relevant)==max(relevant) else f'{min(relevant)}–{max(relevant)}'
        summary=f"Подтверждено {counts['confirmed']}/{total} · в пуле {counts['pooled']} · "+('Applied · ' if depths else '')+f'подтверждения {depth_text}/{required_depth}'
        for phase,label in [('unknown','не найдены'),('unsent','не отправлены'),('failed','не выполнены')]:
            if counts[phase]: summary+=f' · {label}: {counts[phase]}'
        self.progress(summary,'red' if counts['failed'] or counts['unknown'] else 'green' if complete else 'yellow',complete=complete)
