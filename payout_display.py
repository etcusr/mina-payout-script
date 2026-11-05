"""Human progress from verified workflow events; no network or state writes."""
import os
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


def income_totals(summary):
    """Render an accounting snapshot supplied after settlement, without writes."""
    from tabulate import tabulate
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
    print(tabulate(rows,headers=['Показатель','MINA'],disable_numparse=True,colalign=('left','right')))


class PayoutDisplay:
    def __init__(self):
        self.last_rows = {}
        self.health_shown = False
        self.polling = False

    def line(self, text, tone=None):
        print(paint(text,tone), flush=True)

    def plan(self, plan, intents=None, *, required_depth=None):
        self.line('\nПлан выплат · эпоха '+str(plan['epoch']), 'cyan')
        self.line('  №  Адрес получателя                                           Сумма MINA     Сеть MINA  Статус журнала', 'cyan')
        statuses = {i['payment_index']: i['status'] for i in (intents or ())}
        labels = {'submitted': 'отправлен', 'confirmed': 'подтверждён',
                  'failed': 'не выполнен', 'unknown': 'исход неизвестен', 'reserved': 'зарезервирован'}
        for j, p in enumerate(plan['payments']):
            status = labels.get(statuses.get(j), 'не отправлен' if intents is not None else 'статус не загружен')
            self.line(f"{j+1:3}  {account_link(p['recipient'])}  {mina(p['amount_nano']):>14}  {mina(p['fee_nano']):>12}  {paint(status,status_tone(statuses.get(j)))}")
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
            self.line('Эпоха   Комиссия MINA   Свои на кошельке MINA   Расходы на выплаты MINA','cyan')
            for row in summary['credits']:
                self.line(f"{row['epoch']:5}   {mina(row['validator_nano']):>14}   {mina(row['retained_own_nano']):>20}   {mina(row['payout_fees_nano']):>22}")
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
        indexed = {i['payment_index']: i for i in intents}
        confirmed = included = pooled = failed = unknown = unsent = 0
        for j, p in enumerate(plan['payments']):
            i = indexed.get(j)
            if i is None:
                label = 'Не отправлен'; unsent += 1; tone='yellow'
            elif i['status'] == 'failed':
                label = 'Не выполнен; nonce занят другой подтверждённой транзакцией'; failed += 1; tone='red'
            else:
                matches = [r for r in chain if same_payment(i, r) and r.get('on_best_chain') is True and r.get('status') == 'applied']
                if len(matches) == 1:
                    depth = matches[0]['depth']
                    mature = depth >= required_depth
                    confirmed += int(mature); included += int(not mature); tone='green' if mature else 'yellow'
                    label = f"Applied · подтверждения {depth}/{required_depth}"+(' · готово' if mature else ' · ожидание')
                elif any(same_payment(i, r) for r in pool):
                    label = 'В пуле · ожидает блока'; pooled += 1; tone='yellow'
                else:
                    label = 'Пока не найден в цепочке/пуле · повторная отправка запрещена'; unknown += 1; tone='red'
            if self.last_rows.get(j) != label:
                self.line(f"  {j+1:2}. {account_link(p['recipient'])} · {mina(p['amount_nano'])} MINA · {paint(label,tone)}")
                self.last_rows[j] = label
        total = len(plan['payments'])
        filled = 20*confirmed//total if total else 20
        bar = '█'*filled+'░'*(20-filled)
        self.line(f"[{time.strftime('%H:%M:%S')}] [{bar}] Подтверждено {confirmed}/{total} · в цепочке ждут глубины: {included} · в пуле: {pooled} · не найдены: {unknown} · не отправлены: {unsent} · не выполнены: {failed}",'red' if failed or unknown else 'green' if confirmed==total else 'yellow')
