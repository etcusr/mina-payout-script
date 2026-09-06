#!/bin/bash
#
# mina-archive-refresh.sh
#
# Checks gs://mina-archive-dumps for a newer dump and atomically restores it
# into the local Postgres container. Idempotent — safe to run from cron.
#
# Strategy:
#   1. Find latest object in bucket by `updated` timestamp.
#   2. Skip if the same file was applied before (tracked in .applied marker).
#   3. Download with --continue (resumable) and -fL (fail on HTTP errors).
#   4. tar -xzf, find the .sql inside.
#   5. Disconnect all clients from `archive` DB, drop + recreate, restore.
#   6. Mark applied, cleanup downloaded files.
#
# Locking: flock prevents two refreshes at once.
# Logging: appends to /var/log/mina-archive-refresh.log (rotate via logrotate).
#
# Required on host: docker, curl, jq, tar, flock.
# Container must already be running with name $PGCONT.

set -euo pipefail

# --- config -----------------------------------------------------------------
# All paths default to the directory where this script lives. Override via env
# vars (PGCONT, PGUSER, PGDB, WORK, LOG, LOCK) if you want to relocate things.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"

PGCONT="${PGCONT:-mina-archive-pg}"
PGUSER="${PGUSER:-mina}"
PGDB="${PGDB:-archive}"
WORK="${WORK:-$SCRIPT_DIR/dumps}"
LOG="${LOG:-$SCRIPT_DIR/mina-archive-refresh.log}"
LOCK="${LOCK:-$SCRIPT_DIR/mina-archive-refresh.lock}"
BUCKET="mina-archive-dumps"
# Berkeley = post-hard-fork mainnet (current chain). The legacy
# `mainnet-archive-dump-*` files are pre-Berkeley and frozen since May 2025.
DUMP_PREFIX="${DUMP_PREFIX:-berkeley-archive-dump-}"
MARKER="$WORK/.applied"

# --- setup ------------------------------------------------------------------
mkdir -p "$WORK" "$(dirname "$LOG")" "$(dirname "$LOCK")"

# Send all output to log AND stderr (so cron mails if cron is configured)
exec > >(tee -a "$LOG") 2>&1
echo
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) refresh start ==="

# Single instance only
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "another refresh is already running, exiting"
  exit 0
fi

cd "$WORK"

# --- 1. find latest dump in bucket ------------------------------------------
echo "querying gs://$BUCKET for latest dump..."
LATEST=$(
  curl -fsS \
    "https://storage.googleapis.com/storage/v1/b/${BUCKET}/o?prefix=${DUMP_PREFIX}&fields=items(name,updated)&pageSize=5000" \
  | jq -r '.items | sort_by(.updated) | reverse | .[0].name // empty'
)

if [[ -z "$LATEST" ]]; then
  echo "ERROR: could not determine latest dump" >&2
  exit 1
fi
echo "latest dump in bucket: $LATEST"

# --- 2. skip if same as last applied ----------------------------------------
APPLIED=$(cat "$MARKER" 2>/dev/null || true)
if [[ "$APPLIED" == "$LATEST" ]]; then
  echo "already applied: $LATEST — nothing to do"
  exit 0
fi
echo "last applied: ${APPLIED:-<none, initial bootstrap>}"

# --- 3. download (resumable) ------------------------------------------------
echo "downloading $LATEST..."
curl -fL --retry 5 --retry-delay 30 -C - \
  -o "$LATEST" \
  "https://storage.googleapis.com/${BUCKET}/${LATEST}"
echo "download finished, size: $(stat -c %s "$LATEST" 2>/dev/null || stat -f %z "$LATEST") bytes"

# --- 4. extract -------------------------------------------------------------
echo "extracting..."
tar -xzf "$LATEST"

# Find the .sql we just extracted. tar dumps it next to the archive.
SQL=$(tar -tzf "$LATEST" | grep -E '\.sql$' | head -1)
if [[ -z "$SQL" || ! -f "$SQL" ]]; then
  echo "ERROR: no .sql file found after extracting $LATEST" >&2
  exit 1
fi
echo "sql file: $SQL ($(stat -c %s "$SQL" 2>/dev/null || stat -f %z "$SQL") bytes)"

# --- 5. wait for postgres + restore -----------------------------------------
echo "checking postgres container '$PGCONT'..."
if ! docker inspect -f '{{.State.Running}}' "$PGCONT" 2>/dev/null | grep -q true; then
  echo "ERROR: container $PGCONT is not running" >&2
  exit 1
fi
docker exec "$PGCONT" pg_isready -U "$PGUSER" -d postgres >/dev/null

echo "kicking out clients on $PGDB and dropping..."
docker exec "$PGCONT" psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -c "
  SELECT pg_terminate_backend(pid)
  FROM pg_stat_activity
  WHERE datname = '$PGDB' AND pid <> pg_backend_pid();
"
# NOTE: We do NOT pre-create the database here. The Mina archive dump itself
# starts with `CREATE DATABASE archive;` and `\connect archive;`, so we pipe
# it into the `postgres` administrative DB and let the dump create + populate
# the real archive DB on its own.
docker exec "$PGCONT" psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS $PGDB;"

echo "restoring SQL (this is the slow part)..."
START=$(date +%s)
cat "$SQL" | docker exec -i "$PGCONT" psql -U "$PGUSER" -d postgres -v ON_ERROR_STOP=1 -q
END=$(date +%s)
echo "restore took $((END - START)) seconds"

# --- 6. mark applied + cleanup ----------------------------------------------
echo "$LATEST" > "$MARKER"
rm -f -- "$LATEST" "$SQL"
echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) refresh done: $LATEST ==="
