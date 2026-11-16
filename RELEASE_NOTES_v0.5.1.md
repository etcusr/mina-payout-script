## Clearer tables and quieter transaction confirmation

- Confirmation depth updates no longer repeat every recipient row. The full table appears initially and after all payments are confirmed; status changes are grouped by payment number.
- Interactive terminals update a single progress line with the current and required confirmation depth. Plain logs report changes and a heartbeat once a minute instead of every polling tick.
- Payout plans, balance reports, owner income and confirmation tables now have visible row and column borders. Full addresses, Minascan links, exact amounts and status colors are preserved.
- Missing proofs and decreasing confirmation depth remain visible. A proof with a different transaction hash is not displayed as confirmed.
- Progress output finishes cleanly before connection warnings, errors, timeouts and interruption, including after terminal resizing.

### Validation and updating

252 tests passed in the full installation. The public source copy runs the same suite with 18 expected skips for optional private components. Regression coverage includes depth changes, mixed statuses, terminal resizing, plain logs, connection loss and interruption.

Update the checkout normally. The new display applies to the next command invocation; an already-running process keeps its existing display. No payout journal migration or server restart is required.

## Понятные таблицы и тихое подтверждение транзакций

- При росте глубины подтверждений список получателей больше не печатается заново. Полная таблица показывается в начале и после подтверждения всех платежей; смена статуса объединяется по номерам платежей.
- В терминале обновляется одна строка прогресса с текущей и нужной глубиной. В файлах журнала выводятся изменения и отметка ожидания раз в минуту.
- Добавлены границы строк и колонок в таблицах расчёта, плана выплат, дохода владельца и подтверждений. Полные адреса, ссылки Minascan, точные суммы и цвета сохранены.
- Потеря подтверждения и уменьшение глубины остаются заметными. Проверка другого хеша транзакции не показывает ложного подтверждения.
- Исправлено завершение строки прогресса при потере связи, ошибках, таймауте, прерывании и изменении размера окна.

Проверено: 252 теста в полной установке; в публичной копии — тот же набор с 18 ожидаемыми пропусками для приватных компонентов. Новое оформление появляется при следующем запуске команды. Миграция журнала и перезапуск сервера не требуются.
