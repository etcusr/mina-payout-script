#!/bin/bash
# Robust missing-blocks guardian for Mina archive.
#
# Replaces the official Foundation script which:
#   - hardcodes a single S3 bucket (Foundation killed mainnet S3 in 2026)
#   - retries broken blocks infinitely, spamming logs
#
# What this does:
#   1. Asks the local mina daemon for the last N canonical (height, state_hash) pairs
#   2. For each pair missing from Postgres, tries multiple CDN sources in order
#   3. Validates the JSON before handing to mina-archive-blocks
#   4. Permanently failed blocks go to a blacklist so we don't loop forever
#
# Required env:
#   PG_CONN     postgres://user:pass@host:port/db
#   DAEMON_URL  http://mina_node:3085/graphql      (default)
#
# Optional env:
#   GUARDIAN_LOOKBACK  how many recent blocks to scan (default 290 == K-depth on mainnet)

set -u

PG_CONN="${PG_CONN:?PG_CONN required}"
DAEMON_URL="${DAEMON_URL:-http://mina_node:3085/graphql}"
GUARDIAN_LOOKBACK="${GUARDIAN_LOOKBACK:-290}"

# Ordered list of precomputed-block CDNs. First one wins. Add more on the right
# if Foundation kills another bucket — script will just try the next.
SOURCES=(
  "https://storage.googleapis.com/mina_network_block_data"
  "https://673156464838-mina-precomputed-blocks.s3.us-west-2.amazonaws.com/mainnet"
)

# Blacklist file persists across iterations (lives in /tmp inside container,
# which is mounted from ./guardian-tmp on the host — survives container restart).
FAILED_FILE="/tmp/guardian-failed.txt"
touch "$FAILED_FILE"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

# --- 1. Pull recent best-chain from daemon ---------------------------------

log "Querying daemon bestChain(maxLength=$GUARDIAN_LOOKBACK)..."
chain_raw=$(curl -fsS --max-time 20 "$DAEMON_URL" \
  -H 'Content-Type: application/json' \
  -d "{\"query\":\"{ bestChain(maxLength: $GUARDIAN_LOOKBACK) { stateHash protocolState { consensusState { blockHeight } } } }\"}" \
  2>/dev/null || true)

if [[ -z "$chain_raw" ]] || [[ "$chain_raw" == *'"errors"'* ]]; then
  log "Daemon unreachable or returned error — skipping this iteration"
  [[ -n "$chain_raw" ]] && log "Response was: ${chain_raw:0:300}"
  exit 0
fi

# Parse (height, hash) pairs. Try python3 first (clean), fall back to grep/sed.
if command -v python3 >/dev/null 2>&1; then
  mapping=$(echo "$chain_raw" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)['data']['bestChain']
    for b in data:
        h = b['protocolState']['consensusState']['blockHeight']
        s = b['stateHash']
        print(f'{h} {s}')
except Exception as e:
    sys.stderr.write(f'parse error: {e}\n')
    sys.exit(1)
")
else
  # Fallback: bash + grep + sed. Relies on JSON structure being stable.
  mapping=$(echo "$chain_raw" \
    | grep -oE '"stateHash":"[^"]*"|"blockHeight":"[^"]*"' \
    | sed -E 's/"(stateHash|blockHeight)":"//; s/"$//' \
    | paste -d' ' - - \
    | awk '{print $2, $1}')
fi

if [[ -z "$mapping" ]]; then
  log "Failed to parse daemon response"
  exit 1
fi

total=$(echo "$mapping" | grep -c .)
log "Got $total (height, state_hash) pairs from daemon"

# --- 2. Filter to what's actually missing in Postgres ----------------------

TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT
to_fetch="$TMPDIR/to_fetch.txt"
: > "$to_fetch"

# Build set of hashes daemon thinks are canonical
echo "$mapping" | awk '{print $2}' | sort -u > "$TMPDIR/daemon_hashes.txt"

# Build set of hashes already present in archive
psql "$PG_CONN" -tAc "
  SELECT state_hash FROM blocks
  WHERE state_hash = ANY (string_to_array(\$\$$(cat "$TMPDIR/daemon_hashes.txt" | tr '\n' ',' | sed 's/,$//')\$\$, ','))
" 2>/dev/null | sort -u > "$TMPDIR/archive_hashes.txt" || true

# Missing = daemon - archive
comm -23 "$TMPDIR/daemon_hashes.txt" "$TMPDIR/archive_hashes.txt" > "$TMPDIR/missing_hashes.txt"

# Rebuild as (height, hash) lines, skipping blacklisted ones
while IFS=' ' read -r height state_hash; do
  [[ -z "$height" || -z "$state_hash" ]] && continue
  grep -Fxq "$state_hash" "$TMPDIR/missing_hashes.txt" || continue
  grep -q "^${height} ${state_hash}" "$FAILED_FILE" && continue
  echo "$height $state_hash" >> "$to_fetch"
done <<< "$mapping"

missing_count=$(wc -l < "$to_fetch" | tr -d ' ')
log "$missing_count blocks need fetching (after blacklist filtering)"

if [[ "$missing_count" -eq 0 ]]; then
  log "Nothing to do."
  exit 0
fi

# --- 3. Fetch + insert each missing block ----------------------------------

inserted=0
failed=0

while IFS=' ' read -r height state_hash; do
  [[ -z "$height" ]] && continue

  file="$TMPDIR/mainnet-${height}-${state_hash}.json"
  fetched=""
  for src in "${SOURCES[@]}"; do
    url="${src}/mainnet-${height}-${state_hash}.json"
    if curl -fsSL --max-time 30 "$url" -o "$file" 2>/dev/null; then
      # Sanity check: non-empty + looks like JSON
      if [[ -s "$file" ]] && head -c 1 "$file" | grep -q '{'; then
        fetched="$file"
        break
      fi
    fi
  done

  if [[ -z "$fetched" ]]; then
    log "  $height ($state_hash): NOT FOUND in any source → blacklist"
    echo "${height} ${state_hash} not-found $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$FAILED_FILE"
    failed=$((failed + 1))
    continue
  fi

  if mina-archive-blocks --precomputed --archive-uri "$PG_CONN" "$fetched" >/dev/null 2>&1; then
    log "  $height: inserted"
    inserted=$((inserted + 1))
  else
    log "  $height ($state_hash): insert FAILED → blacklist"
    echo "${height} ${state_hash} insert-failed $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$FAILED_FILE"
    failed=$((failed + 1))
  fi
  rm -f "$file"
done < "$to_fetch"

log "Iteration done: inserted=$inserted, failed=$failed, blacklist size=$(wc -l < "$FAILED_FILE" | tr -d ' ')"
