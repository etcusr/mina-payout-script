# Архивная нода Mina

[English](README.md) · **Русский**

Собственная архивная нода, полностью отделённая от block producer'а. Скрипты
выплат берут данные о блоках из её Postgres, а не из публичных API эксплореров —
те имеют свойство исчезать.

## Архитектура

Стек живёт в отдельной docker-сети `mina-net`. Block producer обычно находится в
сети `bridge` по умолчанию и не затрагивается вообще, так что его аптайм не
страдает.

```
[bridge - не трогаем]
  └─ mina                 твой block producer

[mina-net - этот стек]
  ├─ postgres             архивная БД, слушает 127.0.0.1:5432
  ├─ bootstrap_db         one-shot: качает и накатывает mainnet-дамп
  ├─ mina_archive         слушает 3086, пишет входящие блоки в Postgres
  ├─ mina_node            non-producer daemon, синхронит цепь, кормит архив
  └─ missing_blocks_guardian
                          дотягивает пропуски из CDN с precomputed-блоками
```

## Файлы

```
archive/
├── docker-compose.yml       весь стек
├── scripts/
│   └── blocks-guardian.sh   заполнение пропусков с fallback по CDN (см. ниже)
├── backfill-epoch.sh        массовый импорт исторического диапазона высот
├── mina-archive-refresh.sh  накатить свежий дамп, когда выйдет новый
├── init-scripts/            legacy, больше не нужен
├── pgdata/                  данные Postgres (создаётся автоматически)
├── follower-config/         .mina-config фолловера (создаётся автоматически)
└── daemon-restart.sh        DEPRECATED, оставлен для истории
```

## Установка

### 1. Залить стек на сервер

```bash
rsync -av --exclude pgdata --exclude follower-config --exclude cache \
  ./archive/ your-node:~/mina-archive/
```

### 2. Открыть порт libp2p

Фолловеру нужны входящие libp2p-соединения. Он использует **8303**, чтобы не
конфликтовать с продюсером, который обычно занимает 8302.

```bash
sudo ufw allow 8303/tcp
```

### 3. Запустить

```bash
cd ~/mina-archive
docker compose up -d
docker compose ps
```

`bootstrap_db` скачает mainnet-дамп (несколько гигабайт) и накатит его. На первом
запуске это надолго; остальные контейнеры ждут его завершения.

### 4. Проверить

```bash
# Таблицы на месте
docker exec mina-archive-pg psql -U postgres -d archive -c '\dt' | head -30

# Archive-процесс слушает
docker logs mina-archive 2>&1 | tail -20

# Фолловер синхронится
docker logs --tail 30 -f mina-follower

# Блоки приходят
watch -n 10 "docker exec mina-archive-pg psql -U postgres -d archive -c \
  \"SELECT COUNT(*) AS blocks, MAX(height) AS tip FROM blocks;\""
```

Фолловер догоняет текущий tip за несколько часов. После этого каждый новый блок
попадает в Postgres автоматически.

## Про block guardian

Mina Foundation поставляет скрипт `missing-blocks-guardian`, который дотягивает
пропущенные блоки из бакета с precomputed-блоками. В нём жёстко зашит один
S3-бакет, и когда Foundation этот бакет удалила, скрипт начал бесконечно
перезапрашивать один и тот же недоступный блок — сотни строк лога в секунду, без
какого-либо прогресса.

`scripts/blocks-guardian.sh` его заменяет:

- пробует несколько зеркал по порядку (сначала GCS, потом S3)
- валидирует JSON перед передачей в `mina-archive-blocks`
- заносит блоки, недоступные нигде, в blacklist вместо зацикливания
- спрашивает у демона, каких блоков реально не хватает, а не угадывает

Чтобы добавить ещё зеркало — допиши URL в массив `SOURCES` в этом скрипте.

## Подключение скриптов выплат

Скрипты обращаются к `127.0.0.1`, так что пробрось оба порта со своей машины:

```bash
./scripts/tunnel.sh your-node --bg
```

И в `config.yml`:

```yaml
ARCHIVE_DB_URL: "postgresql://postgres:<пароль>@127.0.0.1:5432/archive"
```

Пароль тот, что стоит в `POSTGRES_PASSWORD` в `docker-compose.yml`. По умолчанию
в закоммиченном файле это `postgres` — приемлемо, пока порт привязан только к
`127.0.0.1`. Если будешь выставлять базу наружу, обязательно смени.

## Обслуживание

```bash
# Бэкап
docker exec mina-archive-pg pg_dump -U postgres archive | gzip > archive-$(date +%F).sql.gz

# Размер базы
docker exec mina-archive-pg psql -U postgres -d archive -c \
  "SELECT pg_size_pretty(pg_database_size('archive'));"

# Остановить архивный стек (продюсер не затрагивается)
cd ~/mina-archive && docker compose down

# Снести всё вместе с данными
cd ~/mina-archive && docker compose down -v && rm -rf pgdata follower-config
```

### Переход на свежий дамп

Foundation публикует mainnet-дамп примерно раз в месяц. Чтобы пересобраться на
новом, обнови `DUMP_DATE` в `docker-compose.yml` и пересоздай базу, либо
воспользуйся `mina-archive-refresh.sh`.

### Импорт исторического диапазона

`backfill-epoch.sh` тянет диапазон высот напрямую из бакета precomputed-блоков и
импортирует его — полезно, когда нужны эпохи старше твоего дампа. Пароль базы
скрипт берёт из окружения:

```bash
PGPASSWORD=... ./backfill-epoch.sh <min-height> <max-height>
```

## Откат

```bash
cd ~/mina-archive
docker compose down
docker ps | grep mina    # должен остаться только продюсер
```

Контейнер продюсера всё это время находится в сети bridge и этим стеком никак не
переконфигурируется.
