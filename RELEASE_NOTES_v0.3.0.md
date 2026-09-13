*English · [Русский](#русский)*

## Mesa hard fork, self-hosted data, and payout safety

This release rebuilds the data layer on a self-hosted archive node, adapts
everything to the Mesa hard fork, and adds the guards that keep a bad payout
file from becoming a bad payout.

### Mesa hard fork support

The 2026-09-03 upgrade changed four constants this script assumed. All of them
are now detected rather than hardcoded:

- 90-second slots (was 180), epochs of 7.44 days (was 14.9)
- Coinbase of 360 MINA (was 720) — `getBaseCoinbase()` reads the most frequent
  coinbase value for the current protocol era instead of assuming one
- Epoch numbering reset to 0 — blocks are filtered by `protocol_version_id`, so
  epoch 0 of Mesa is never confused with epoch 0 of Berkeley
- `global_slot_since_hard_fork` reset — slot↔timestamp conversion reads the
  daemon's genesis timestamp

### Self-hosted data

Every third-party API this project used is now dead: `graphql.minaexplorer.com`
stopped resolving, `api.minastakes.com` returns 500s, and the Foundation's
precomputed-block bucket was deleted.

`GraphQL.py` now reads the archive node's Postgres directly, with the daemon's
GraphQL for live state. The only remaining external dependency is the public GCS
staking-ledger bucket.

- `archive/` — Docker Compose stack for the archive node
- `archive/scripts/blocks-guardian.sh` — replaces the Foundation's missing-block
  script, which looped forever on blocks that no longer exist anywhere. Tries
  multiple mirrors, validates the JSON, and blacklists blocks that are genuinely
  gone instead of re-requesting them.

### Correct confirmation counting

Confirmations were computed as `tip_height − block_height`, which reports an
orphaned block as deeply confirmed: it counts the height difference, not whether
the block is still on the chain. A block with 302 "confirmations" turned out to
have zero descendants.

Confirmations are now real descendants, resolved by walking the parent chain
from the best tip. An orphan shows as lost immediately rather than at K=290, so
you can pay out at 20 confirmations instead of waiting three days.

### Payout safety

`send_payout.py` runs pre-flight checks and aborts rather than sending a partial
round:

- **Duplicate destinations in the CSV** — a payout file holds one row per
  address; repeats mean it was appended to across runs
- **Total against the live wallet balance** — a short balance stops the run
  partway and leaves the epoch half paid
- **An existing `sended_txs_e<N>.csv`** — this epoch was already paid

New flags: `--dry-run` (checks only), `--resume` (continue an interrupted round,
skipping addresses already paid), `--yes` (skip the confirmation prompt). The
wallet is now relocked in a `finally` block, so it stays locked even when a send
raises.

### Ledger and reconciliation

`payout_ledger.json` carries per-address differences into the next epoch:
negative means the address was overpaid and its next payout is reduced,
positive means it was underpaid and gets topped up. Debts larger than the next
payout carry forward again until cleared.

```bash
python3 calc_rewards.py --epoch 0        # refresh the payout file
python3 reconcile.py --epoch 0           # show sent vs owed per address
python3 reconcile.py --epoch 0 --commit  # store the deltas
```

`calc_rewards.py` applies the balances automatically on the next run.

### VRF slot prediction

`vrf/probe.py` evaluates the VRF for every delegator across an epoch and reports
which slots the validator wins, before they happen. Validated against real
blocks: all ten predicted slots produced at the predicted minute.

```bash
python3 vrf/probe.py --epoch 1        # scan and cache
python3 calc_rewards.py --epoch 1 --vrf  # reuse the cache in the report
```

Staking ledgers are downloaded automatically when missing. The daemon is
single-threaded, so throughput caps around 650 evaluations/second regardless of
client threads.

### Configuration and packaging

- No epoch in the config — `calc_rewards.py`, `send_payout.py` take `--epoch`
- **No wallet password in the config** — prompted at runtime with hidden input,
  so it never reaches the shell history or disk
- `PAYOUT_REDIRECTS` — reward redirection moved from the script body into the
  config, supporting both full redirects and percentage splits
- `mina_client.py` replaces the `coda-python-client` git dependency, whose
  repository no longer exists
- `scripts/install.sh`, `scripts/tunnel.sh`, `scripts/aliases.sh`

### Documentation

Full bilingual documentation with a language switcher: [English](README.md) ·
[Русский](README.ru.md), plus separate docs for the archive stack. All code
comments are in English.

---

## Русский

Этот релиз переносит слой данных на собственную архивную ноду, адаптирует всё
под хардфорк Mesa и добавляет проверки, которые не дают испорченному
payout-файлу превратиться в испорченную выплату.

### Поддержка хардфорка Mesa

Обновление 3 сентября 2026 поменяло четыре константы, на которые опирался
скрипт. Все они теперь определяются, а не зашиты в код:

- слоты по 90 секунд (было 180), эпохи по 7.44 дня (было 14.9)
- coinbase 360 MINA (было 720) — `getBaseCoinbase()` берёт самое частое
  значение coinbase для текущей эры протокола, а не предполагает его
- нумерация эпох сброшена на 0 — блоки фильтруются по `protocol_version_id`,
  так что эпоха 0 Mesa не путается с эпохой 0 Berkeley
- сброшен `global_slot_since_hard_fork` — перевод слота в время читает
  genesis timestamp у демона

### Свои данные вместо чужих API

Все сторонние API, на которых держался проект, умерли:
`graphql.minaexplorer.com` перестал резолвиться, `api.minastakes.com` отдаёт
500-е, а бакет Foundation с precomputed-блоками удалили.

`GraphQL.py` теперь читает Postgres архивной ноды напрямую, а живое состояние
берёт из GraphQL демона. Единственная оставшаяся внешняя зависимость —
публичный GCS-бакет со стейкинг-леджерами.

- `archive/` — Docker Compose для архивной ноды
- `archive/scripts/blocks-guardian.sh` — замена скрипту Foundation, который
  бесконечно крутился на блоках, которых больше нигде нет. Перебирает
  несколько зеркал, проверяет JSON и заносит реально пропавшие блоки в
  чёрный список вместо того, чтобы запрашивать их снова

### Правильный подсчёт подтверждений

Подтверждения считались как `tip_height − block_height`, и такой счёт
показывает orphaned-блок глубоко подтверждённым: он меряет разницу высот, а не
то, остался ли блок в цепочке. Блок с «302 подтверждениями» на деле не имел ни
одного потомка.

Теперь подтверждения — это реальные потомки, определяются проходом по цепочке
родителей от лучшего типа. Orphan виден сразу, а не на K=290, так что платить
можно на 20 подтверждениях, а не ждать трое суток.

### Безопасность выплат

`send_payout.py` прогоняет проверки до отправки и прерывается, а не отправляет
половину раунда:

- **Повторяющиеся адреса в CSV** — в payout-файле одна строка на адрес, повторы
  означают, что файл дописывался между запусками
- **Сумма против живого баланса кошелька** — нехватки хватит, чтобы оборваться
  на середине и оставить эпоху наполовину оплаченной
- **Существующий `sended_txs_e<N>.csv`** — эпоха уже оплачена

Новые флаги: `--dry-run` (только проверки), `--resume` (дослать прерванный
раунд, пропуская уже оплаченные адреса), `--yes` (без подтверждения). Кошелёк
блокируется в `finally` — он останется закрытым, даже если отправка упадёт.

### Реестр и сверка

`payout_ledger.json` переносит разницу по адресам на следующую эпоху:
отрицательное сальдо означает переплату и уменьшает следующую выплату,
положительное — недоплату и добавляется сверху. Долг больше следующей выплаты
переносится дальше, пока не закроется.

```bash
python3 calc_rewards.py --epoch 0        # обновить payout-файл
python3 reconcile.py --epoch 0           # отправлено против причитавшегося
python3 reconcile.py --epoch 0 --commit  # записать разницу
```

`calc_rewards.py` применяет сальдо сам при следующем запуске.

### Предсказание слотов через VRF

`vrf/probe.py` считает VRF по всем делегаторам на эпоху вперёд и показывает,
какие слоты валидатор выигрывает, до того как они наступят. Проверено на
реальных блоках: все десять предсказанных слотов дали блок в предсказанную
минуту.

```bash
python3 vrf/probe.py --epoch 1           # просканировать и закэшировать
python3 calc_rewards.py --epoch 1 --vrf  # переиспользовать кэш в отчёте
```

Стейкинг-леджеры скачиваются сами, если их нет. Демон однопоточный, так что
потолок — около 650 вычислений в секунду независимо от числа потоков клиента.

### Конфигурация и упаковка

- Эпохи в конфиге больше нет — `calc_rewards.py` и `send_payout.py` принимают
  `--epoch`
- **Пароля кошелька в конфиге нет** — спрашивается при запуске скрытым вводом,
  так что не попадает ни в историю шелла, ни на диск
- `PAYOUT_REDIRECTS` — перераспределение наград переехало из тела скрипта в
  конфиг, поддерживает и полный редирект, и деление по процентам
- `mina_client.py` заменяет git-зависимость `coda-python-client`, чей
  репозиторий больше не существует
- `scripts/install.sh`, `scripts/tunnel.sh`, `scripts/aliases.sh`

### Документация

Полная двуязычная документация с переключателем языка: [English](README.md) ·
[Русский](README.ru.md), плюс отдельные доки по архивному стеку. Комментарии в
коде — на английском.
