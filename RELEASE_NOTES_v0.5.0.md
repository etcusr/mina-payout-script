## Historical archive recovery and resilient payout checks

### Changes

- Guardian now searches the entire canonical/pending archive for missing parent blocks, independently of the recent daemon window. It walks exact parent hashes and repairs bounded chunks through Mina's official importer.
- Unavailable blocks use persistent retry backoff instead of a permanent blacklist. Overlap protection, download/import budgets, and receipt plus database verification prevent false success reports.
- Full-history audits report canonical gaps, duplicate heights, broken parent links, and stale pending blocks. `--check` performs an audit without downloading or importing blocks.
- Payout proof capture automatically retries up to three times when a new block extends the checked chain. Reorganizations and network/era changes still stop the operation; transaction submission is never retried by this mechanism.
- Staking-ledger loading accepts binary `token_symbol` metadata while retaining strict validation of payout inputs and exact decimal amounts.
- Preview output now correctly explains that no sendable plan was saved, including previews of closed epochs.

### Updating

Update the payout checkout normally. For an existing archive installation, copy both `archive/scripts/blocks_guardian.py` and `archive/scripts/blocks-guardian.sh` into its mounted scripts directory, installing the Python helper first. The guardian requires Python 3.9+, `psql`, and `mina-archive-blocks`; the existing 600-second loop remains compatible. No payout journal migration is required.

See [archive documentation](archive/README.md) for limits, retry settings and audit commands.

### Validation

- 238 tests passed in the full local installation.
- On an isolated copy of a real archive backup, the official importer restored 215 missing blocks in batches of 100, 100 and 15. All 505 canonical height gaps cleared, and the next run imported nothing.
- The installed guardian's regular cycle completed with no detected historical gaps or broken links.

## Восстановление старых пропусков и устойчивая сверка выплат

- Guardian ищет отсутствующих родителей по всей истории canonical/pending, а не только среди последних блоков ноды. Восстановление выполняется порциями через штатный импортёр Mina.
- Постоянный чёрный список заменён повторными попытками с паузой. Добавлены защита от параллельных запусков, ограничения загрузки и проверка результата импорта по квитанциям и БД.
- Полная проверка выявляет пропуски canonical, дубли высот, нарушенные связи и старые pending. Режим `--check` работает без скачивания и импорта.
- Появление нового блока во время сверки автоматически запускает повторную проверку, максимум три попытки. Смена цепочки, сети или эры останавливает операцию. Отправка транзакций этим механизмом не повторяется.
- Исправлено чтение staking ledger с бинарным `token_symbol`; проверка адресов и сумм остаётся строгой. Уточнено сообщение предварительного расчёта закрытой эпохи.

При обновлении архива установите оба файла guardian, сначала Python-файл. Существующий цикл раз в 600 секунд совместим; миграция журнала выплат не нужна. Подробности — в [документации архива](archive/README.ru.md).

Проверено: 238 локальных тестов; восстановление 215 настоящих блоков на отдельной копии БД за три прохода; повторный запуск без повторного импорта; успешный штатный цикл установленного guardian.
