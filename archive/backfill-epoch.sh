#!/bin/bash
#
# backfill-epoch.sh — импорт исторических precomputed-блоков в archive Postgres.
#
# Алгоритм:
#   1) GCS bucket mina_network_block_data → listing блоков в нужном диапазоне высот
#   2) Параллельная скачка (20 потоков) на сервер
#   3) Импорт через `docker run --rm minaprotocol/mina-archive ... mina-archive-blocks --precomputed`
#   4) Проверка количества блоков в Postgres
#
# Использование:
#   ./backfill-epoch.sh <min_height> <max_height>
#
# Пример для epoch 46:
#   ./backfill-epoch.sh 522526 525344

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <min_height> <max_height>" >&2
  exit 1
fi

H_MIN="$1"
H_MAX="$2"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
WORK="${WORK:-$SCRIPT_DIR/backfill-$H_MIN-$H_MAX}"
LOG="${LOG:-$SCRIPT_DIR/backfill.log}"

PGUSER="${PGUSER:-mina}"
PGPASSWORD="${PGPASSWORD:-IYF32rfIYFFOr38o7f2}"
PGDB="${PGDB:-archive}"
PG_HOST_IN_NET="${PG_HOST_IN_NET:-postgres}"   # имя сервиса в mina-net
NET="${NET:-mina-net}"
ARCHIVE_IMAGE="${ARCHIVE_IMAGE:-minaprotocol/mina-archive:3.0.1-4e62fc2-focal}"
PG_URI="postgres://${PGUSER}:${PGPASSWORD}@${PG_HOST_IN_NET}:5432/${PGDB}"

PARALLEL="${PARALLEL:-20}"

exec > >(tee -a "$LOG") 2>&1
echo
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) backfill start for heights $H_MIN..$H_MAX ==="
mkdir -p "$WORK"
cd "$WORK"

# --- 1. listing --------------------------------------------------------------
echo
echo "=== 1. GCS listing блоков $H_MIN..$H_MAX ==="
LISTING_URL="https://storage.googleapis.com/storage/v1/b/mina_network_block_data/o"
LISTING_PARAMS=$(printf 'prefix=mainnet-&startOffset=mainnet-%s-&endOffset=mainnet-%s-&fields=items%%28name,size%%29&maxResults=20000' \
                  "$H_MIN" "$((H_MAX + 1))")
curl -fsS "${LISTING_URL}?${LISTING_PARAMS}" > listing.json

TOTAL=$(jq '.items | length' listing.json)
TOTAL_BYTES=$(jq '[.items[].size | tonumber] | add' listing.json)
echo "Найдено блоков: $TOTAL"
echo "Суммарный объём: $(numfmt --to=iec --suffix=B "$TOTAL_BYTES")"

# Список имён файлов для скачки
jq -r '.items[].name' listing.json > names.txt

# --- 2. скачка --------------------------------------------------------------
echo
echo "=== 2. Параллельная скачка ($PARALLEL потоков) ==="
START=$(date +%s)

# Уже скачанные пропускаем (resume по факту: если файл есть и не пустой, скип)
xargs -a names.txt -P "$PARALLEL" -I {} sh -c '
  if [ -s "$1" ]; then
    exit 0
  fi
  curl -fsS -o "$1" "https://storage.googleapis.com/mina_network_block_data/$1"
' _ {}

DOWN_SEC=$(($(date +%s) - START))
echo "Скачано за $DOWN_SEC сек"
ls mainnet-*.json | wc -l
du -sh .

# --- 3. import через mina-archive-blocks ------------------------------------
echo
echo "=== 3. Импорт в Postgres через mina-archive-blocks ==="
START=$(date +%s)

# Передаём files через docker volume. Имена внутри контейнера: /data/mainnet-*.json
# Запускаем БЕЗ --rm чтобы можно было пересмотреть логи; в конце сами удалим.

> ok.txt
> fail.txt

# Если файлов очень много — argv limit. Идём батчами.
BATCH="${BATCH:-300}"
i=0
ls mainnet-*.json > files-all.txt
TOTAL_FILES=$(wc -l < files-all.txt)

split -l "$BATCH" files-all.txt files-batch-

for batch in files-batch-*; do
  i=$((i + 1))
  COUNT=$(wc -l < "$batch")
  echo "  Batch $i: $COUNT files"

  # Превращаем имена в пути /data/...
  awk '{print "/data/" $0}' "$batch" > "${batch}.paths"
  PATHS=$(tr '\n' ' ' < "${batch}.paths")

  docker run --rm \
    --network "$NET" \
    -v "$WORK:/data:ro" \
    "$ARCHIVE_IMAGE" \
    mina-archive-blocks \
      --archive-uri "$PG_URI" \
      --precomputed \
      --successful-files /data/ok.txt \
      --failed-files /data/fail.txt \
      --log-successful false \
      $PATHS \
    2>&1 | tail -10 || echo "  (batch $i exit non-zero; смотри ok.txt/fail.txt)"
done

IMPORT_SEC=$(($(date +%s) - START))
echo "Импорт за $IMPORT_SEC сек"

OK_COUNT=$(wc -l < ok.txt 2>/dev/null || echo 0)
FAIL_COUNT=$(wc -l < fail.txt 2>/dev/null || echo 0)
echo "Успешно: $OK_COUNT, провалов: $FAIL_COUNT"

# --- 4. проверка ------------------------------------------------------------
echo
echo "=== 4. Что в БД после импорта ==="
docker exec mina-archive-pg psql -U "$PGUSER" -d "$PGDB" -c "
  SELECT
    COUNT(*) AS total_blocks,
    SUM(CASE WHEN chain_status='canonical' THEN 1 ELSE 0 END) AS canonical,
    SUM(CASE WHEN chain_status='orphaned' THEN 1 ELSE 0 END) AS orphaned,
    SUM(CASE WHEN chain_status='pending' THEN 1 ELSE 0 END) AS pending,
    MIN(height) AS min_h,
    MAX(height) AS max_h
  FROM blocks
  WHERE height BETWEEN $H_MIN AND $H_MAX;
"

# Сколько блоков твоего пула в этом диапазоне (если задал)
if [[ -n "${POOL_KEY:-}" ]]; then
  docker exec mina-archive-pg psql -U "$PGUSER" -d "$PGDB" -c "
    SELECT
      COUNT(*) AS pool_blocks,
      SUM(CASE WHEN chain_status='canonical' THEN 1 ELSE 0 END) AS pool_canonical
    FROM blocks b JOIN public_keys pk ON pk.id = b.creator_id
    WHERE pk.value = '$POOL_KEY'
      AND b.height BETWEEN $H_MIN AND $H_MAX;
  "
fi

echo
echo "=== Done. ==="
echo "Дальше можно: rm -rf $WORK   (после того как убедился что всё ок)"
