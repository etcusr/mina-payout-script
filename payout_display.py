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
        self.batch_proof_plan = None
        self.last_batch_rows = {}
        self.last_batch_depths = {}
        self.batch_complete = False

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
        if plan.get('kind')=='withdrawal':
            partial=plan.get('withdrawal_limit',{}).get('remaining_income_nano',0)>0
            self.line('\n'+('ЧАСТИЧНЫЙ вывод дохода' if partial else 'Вывод дохода владельца'), 'yellow' if partial else 'cyan')
        else:
            self.line('\nПлан выплат · эпоха '+str(plan['epoch']), 'cyan')
        statuses = {i['payment_index']: i['status'] for i in (intents or ())}
        labels = {'submitted': 'отправлен', 'confirmed': 'подтверждён',
                  'failed': 'не выполнен', 'unknown': 'исход неизвестен', 'reserved': 'зарезервирован'}
        rows=[]
        policy=plan.get('policy',{})
        batch_size=policy['batch_size'] if policy.get('transport')=='zkapp_batch' else None
        for j, p in enumerate(plan['payments']):
            status = labels.get(statuses.get(j), 'не отправлен' if intents is not None else 'статус не загружен')
            rows.append([j+1]+([j//batch_size+1] if batch_size else [])+[account_link(p['recipient']),mina(p['amount_nano']),mina(p['fee_nano']),paint(status,status_tone(statuses.get(j)))])
        if rows:
            self.line(table(rows,['№']+(['Пакет'] if batch_size else [])+['Адрес получателя','Сумма MINA','Доля сети MINA' if batch_size else 'Сеть MINA','Статус журнала'],colalign=('right',)*(2 if batch_size else 1)+('left','right','right','left')))
        amount = sum(p['amount_nano'] for p in plan['payments'])
        fees = sum(p['fee_nano'] for p in plan['payments'])
        self.line(f"Итого: {len(plan['payments'])} платежей · {mina(amount)} MINA")
        if batch_size:
            self.line(f"Пакетов: {(len(rows)+batch_size-1)//batch_size} · до {batch_size} получателей в пакете")
            for start in range(0,len(plan['payments']),batch_size):
                group=plan['payments'][start:start+batch_size]
                self.line(f"Пакет №{start//batch_size+1}: {len(group)} получателей · комиссия {mina(sum(p['fee_nano'] for p in group))} MINA")
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
            rows=[[r['epoch'],mina(r['validator_nano']),mina(r['retained_own_nano']),mina(r['payout_fees_nano']),mina(r.get('delegator_fees_nano',0))] for r in summary['credits']]
            if rows:
                self.line(table(rows,['Эпоха','Комиссия MINA','Свои на кошельке MINA','Сеть за счёт владельца','Сеть за счёт делегаторов'],colalign=('right',)*5))
            self.line('Доход после расходов: '+mina(summary['earned_nano'])+' MINA')
            self.line('Уже выплачено своим адресам как делегаторам: '+mina(sum(r['own_paid_nano'] for r in summary['credits']))+' MINA (повторно в вывод не включается)')
            self.line('Уже выведено с комиссиями: '+mina(summary['withdrawn_with_fees_nano'])+' MINA')
            self.line('Зарезервировано с комиссиями: '+mina(summary['reserved_with_fees_nano'])+' MINA')
            limit=data.get('withdrawal_limit')
            if limit is not None:
                rows=[[label,mina(limit[key])] for label,key in [
                    ('Учтённый доход к выводу','requested_nano'),('Доступный баланс кошелька','liquid_nano'),
                    ('Незавершённые переводы с комиссиями','commitments_nano'),('Резерв делегаторов','reserved_nano'),
                    ('Свободно после резервов','available_nano'),('Вывод с комиссией','withdrawal_total_nano'),
                    ('Останется учтённого дохода','remaining_income_nano')]]
                self.line(table(rows,['Расчёт доступного вывода','MINA'],colalign=('left','right')))
                if limit['remaining_income_nano']>0 and data['amount_nano']>0:
                    self.line('Средств на полный вывод недостаточно. Предлагается ЧАСТИЧНЫЙ вывод; остаток дохода сохранится в БД.','yellow')
            self.line('Получателю: '+mina(data['amount_nano'])+' MINA · комиссия вывода: '+mina(data['fee_nano'])+' MINA','cyan')
        elif event == 'proofs':
            self.proofs(**data)
        elif event == 'batch_proofs':
            self.batch_proofs(**data)

    def batch_proofs(self, plan, batches, required_depth=3):
        """One transaction status per batch; recipients were shown in the plan."""
        self.polling=True
        key=(plan.get('epoch'),plan.get('operation_id'),plan.get('input_fingerprint'),required_depth,
             tuple((p['recipient'],p['amount_nano'],p['fee_nano']) for p in plan['payments']))
        if key!=self.batch_proof_plan:
            self.finish(); self.last_batch_rows={}; self.last_batch_depths={}; self.batch_complete=False
            self.batch_proof_plan=key
        counts=dict(confirmed=0,included=0,pooled=0,failed=0,unknown=0,unsent=0,cancelled=0)
        depths=[]; waiting=[]
        for batch in batches:
            index=batch['index']; proof=batch.get('proof') or {}; depth=proof.get('depth')
            chain_status=proof.get('in_chain_status') or proof.get('status')
            if chain_status in ('applied','failed') and depth is not None:
                mature=depth>=required_depth
                phase=('confirmed' if chain_status=='applied' else 'failed') if mature else 'included'
                label=('Подтверждён' if mature else 'Applied') if chain_status=='applied' else ('Failed' if mature else 'Failed · ожидает глубины')
                tone='green' if phase=='confirmed' else 'red' if chain_status=='failed' else 'yellow'
                depths.append(depth)
                if not mature: waiting.append((index,depth))
            elif proof.get('in_pool'):
                phase='pooled'; label='В пуле'; tone='yellow'
            elif batch['status'] in ('planned','prepared','cancelled','failed'):
                phase={'planned':'unsent','prepared':'unsent','cancelled':'cancelled','failed':'failed'}[batch['status']]
                label={'planned':'Не отправлен','prepared':'Подготовлен','cancelled':'Отменён','failed':'Failed'}[batch['status']]
                tone='red' if phase=='failed' else 'yellow'
            else:
                phase='unknown'; label='исход неизвестен'; tone='red'
            counts[phase]+=1
            state=(phase,label,batch.get('nonce'),batch.get('tx_hash'),proof.get('height'),proof.get('block_hash'))
            if self.last_batch_rows.get(index)!=state:
                self.line(f"Пакет №{index+1}: {label} · nonce={batch.get('nonce') if batch.get('nonce') is not None else '—'} · tx={batch.get('tx_hash') or '—'} · блок {proof.get('height','—')} · подтверждения {depth if depth is not None else '—'}/{required_depth}",tone)
                if phase=='unknown': self.line('Исход неизвестен: повторная отправка запрещена; продолжаю сверку.','red')
            previous=self.last_batch_depths.get(index)
            if previous is not None and depth is not None and depth<previous:
                self.line(f'Глубина снизилась: пакет №{index+1}, {previous} → {depth}. Проверяю свежую цепочку.','yellow')
            self.last_batch_rows[index]=state; self.last_batch_depths[index]=depth
        total=len(batches); complete=counts['confirmed']==total
        summary=f"Подтверждено пакетов {counts['confirmed']}/{total} · в пуле {counts['pooled']}"
        if waiting:
            index,depth=min(waiting,key=lambda item:item[1])
            summary+=f' · пакет №{index+1}: {depth}/{required_depth}'
        elif depths: summary+=f' · подтверждения {min(depths)}/{required_depth}'
        for phase,label in [('unknown','исход неизвестен'),('unsent','не отправлены'),('failed','Failed'),('cancelled','отменены')]:
            if counts[phase]: summary+=f' · {label}: {counts[phase]}'
        if not complete or not self.batch_complete or summary!=self.progress_key:
            self.progress(summary,'red' if counts['failed'] or counts['unknown'] else 'green' if complete else 'yellow',complete=complete)
        self.batch_complete=complete

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
