#!/bin/bash
# Usage: ./calc.sh [epoch] [extra calc_rewards.py flags...]
#
#   ./calc.sh                 current epoch, taken from the local daemon
#   ./calc.sh 0               epoch 0
#   ./calc.sh --vrf           current epoch, with the VRF slot scan
#   ./calc.sh 0 --vrf         epoch 0, with the VRF slot scan
#
# A leading bare number is treated as the epoch; everything else is passed
# through to calc_rewards.py untouched.

set -u

# Resolve the project dir from the script's own location, following symlinks.
# Nothing to update when the project moves or is symlinked from elsewhere.
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
PROJECT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

cd "$PROJECT_DIR" || exit 1

# A leading integer is the epoch; anything else is a flag for calc_rewards.py
EPOCH=""
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  EPOCH="$1"
  shift
fi

# --epoch N / -e N / --epoch=N anywhere in the flags means the epoch is already
# known, so there is no reason to ask the daemon for it. calc_rewards.py gets
# the flags untouched either way; this only decides whether we need the tunnel.
if [ -z "$EPOCH" ]; then
  _prev=""
  for _a in "$@"; do
    case "$_prev" in --epoch|-e) [[ "$_a" =~ ^[0-9]+$ ]] && EPOCH="$_a" ;; esac
    case "$_a" in --epoch=*) EPOCH="${_a#--epoch=}" ;; esac
    _prev="$_a"
  done
  # Passed through below as-is, so don't let it be added a second time.
  if [ -n "$EPOCH" ]; then
    source venv/bin/activate
    exec python3 calc_rewards.py "$@"
  fi
fi

# No epoch given - ask the daemon. It is the authoritative source for the
# active protocol era: after the Mesa fork the epoch numbering restarted at 0,
# so third-party explorers may report a different scheme.
if [ -z "$EPOCH" ]; then
  if ! command -v jq >/dev/null 2>&1; then
    echo "jq is not installed, so the epoch cannot be read from the daemon."
    echo "Install it (brew install jq) or pass the epoch: ./calc.sh N"
    exit 1
  fi
  # 10s, not 3: the daemon is single-threaded, and while a VRF scan is running
  # it can take several seconds to answer even a trivial query.
  _resp=$(curl -s -m 10 http://127.0.0.1:3085/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"{ bestChain(maxLength:1){ protocolState{ consensusState{ epoch } } } }"}')
  EPOCH=$(printf '%s' "$_resp" \
    | jq -r '.data.bestChain[0].protocolState.consensusState.epoch' 2>/dev/null)
fi

if [ -z "$EPOCH" ] || [ "$EPOCH" = "null" ]; then
  if [ -z "${_resp:-}" ]; then
    echo "The daemon on 127.0.0.1:3085 did not answer within 10s."
    echo "Check the tunnel:  nc -z 127.0.0.1 3085 && echo open"
    echo "It also stays busy during a VRF scan - a single-threaded daemon can"
    echo "take longer than that to reply while one is running."
  else
    echo "The daemon answered, but not with an epoch:"
    printf '  %s\n' "$(printf '%s' "$_resp" | head -c 300)"
  fi
  echo
  echo "Either way you can skip the lookup by naming the epoch:"
  echo "  ./calc.sh N          or          ./calc.sh --epoch N"
  exit 1
fi

source venv/bin/activate
python3 calc_rewards.py --epoch "$EPOCH" "$@"
