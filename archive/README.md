# Mina Archive Node Setup

Архив, отдельный от твоего block producer'а. Поднимается тремя контейнерами
на сервере, на изолированной сети `mina-net`. Producer (`mina`) на `bridge` —
его не трогаем вообще, его 19h аптайма сохраняется.

## Архитектура

```
[bridge, как было]
  └─ mina (block producer)  ← uptime сохраняется

[mina-net, новая]
  ├─ mina-follower    non-producer daemon, сам синхронит сеть
  │                   и шлёт каждый блок в --archive-address
  ├─ mina-archive     слушает 3086, парсит, пишет в postgres
  └─ postgres         5432 на хосте (для SSH-туннеля с мака)
```

## Файлы

```
archive/
├── docker-compose.yml      Postgres + mina-archive + mina-follower
├── init-scripts/
│   ├── README.md           инструкция как скачать SQL ниже
│   ├── 00-create_schema.sql   (скачать вручную)
│   └── 01-zkapp_tables.sql
├── follower-config/        ~/.mina-config follower'а (создаётся автоматом)
├── pgdata/                 данные Postgres (создаётся автоматом)
├── daemon-restart.sh       DEPRECATED, не используем
└── README.md               этот файл
```

## Установка

### Шаг 1 — залить файлы на сервер

```bash
# С твоего мака
rsync -av --exclude pgdata --exclude follower-config --exclude '*.log' --exclude '*.lock' --exclude dumps --exclude cache \
  ~/Documents/projects/python/Mina/mina-payout-script/archive/ \
  your-node:~/mina-archive/
```

### Шаг 2 — скачать schema SQL

```bash
ssh your-node
cd ~/mina-archive/init-scripts/

curl -fsSL -o 00-create_schema.sql \
  https://raw.githubusercontent.com/MinaProtocol/mina/4e62fc2/src/app/archive/create_schema.sql

ls -la   # должно быть ~24 KB
```

В 3.0.x все таблицы (включая zkapp_*) уже в одном `create_schema.sql`.
Отдельный `zkapp_tables.sql` появился в более поздних релизах.

### Шаг 3 — открыть порт 8303 на сервере

Чтобы follower-демон мог делать входящие libp2p-соединения:

```bash
# Если у тебя ufw:
sudo ufw allow 8303/tcp
# Или iptables / хостер firewall — нужен публичный 8303 TCP
```

(8302 уже открыт у твоего producer'а, оставляем как есть.)

### Шаг 4 — поднять стек

```bash
cd ~/mina-archive
docker compose up -d

# Через ~30 сек проверь
docker compose ps
# postgres: healthy
# archive:  running
# follower: running
```

### Шаг 5 — проверки

```bash
# Схема накатилась
docker exec mina-archive-pg psql -U mina -d archive -c '\dt' | head -30
# должны быть blocks, public_keys, user_commands, internal_commands, zkapp_*

# Archive слушает
docker logs mina-archive 2>&1 | tail -20
# должно быть про "Initializing archive process" + listening on 3086

# Follower начал sync (займёт 1-3 часа)
docker logs --tail 30 -f mina-follower
# ищем "Block produced/received", "Catching up", "Best tip changed"

# Когда follower поймает текущий tip — увидим в Postgres новые блоки
watch -n 10 "docker exec mina-archive-pg psql -U mina -d archive -c \
  \"SELECT COUNT(*) AS blocks, MAX(height) AS tip FROM blocks;\""
```

## Что дальше

- Через 1-3 часа follower синхронится до текущего tip
- С этого момента **каждый новый блок** автоматом летит в Postgres
- Когда epoch 47 закончится — все её блоки уже у нас в БД
- Тогда переписываем `GraphQL.py` на SQL (см. Phase 3 ниже)

## Phase 3 — переписать GraphQL.py на SQL (когда archive накопит блоки)

1. Обновить SSH-туннель чтобы пробросить 5432:
   ```bash
   alias tunnel_mina='tmux new -s minatun -d "ssh your-node -N -L 3085:localhost:3085 -L 5432:localhost:5432"'
   ```

2. В `config.yml` добавить:
   ```yaml
   ARCHIVE_DB_URL: "postgresql://mina:IYF32rfIYFFOr38o7f2@127.0.0.1:5432/archive"
   ```

3. В venv поставить psycopg2:
   ```bash
   source venv/bin/activate
   pip install psycopg2-binary
   ```

4. Скажешь — перепишу `GraphQL.getBlocks` на SQL к archive-схеме.
   `getStakingLedger` можно оставить на GCS bucket — 286МБ JSON-а раз в эпоху это терпимо.

## Бэкап и обслуживание

```bash
# Дамп
docker exec mina-archive-pg pg_dump -U mina archive | gzip > archive-$(date +%F).sql.gz

# Размер БД
docker exec mina-archive-pg psql -U mina -d archive -c \
  "SELECT pg_size_pretty(pg_database_size('archive'));"

# Остановить ВСЕ компоненты архива (producer не затрагивается)
cd ~/mina-archive && docker compose down

# Полностью снести (с потерей данных)
cd ~/mina-archive && docker compose down -v && rm -rf pgdata follower-config
```

## Откат / если что-то пошло не так

```bash
cd ~/mina-archive
docker compose down
# producer-контейнер `mina` остался в бridge-сети, работает как работал
docker ps | grep mina   # должен быть только producer
```
