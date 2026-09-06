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

# No epoch given - ask the daemon. It is the authoritative source for the
# active protocol era: after the Mesa fork the epoch numbering restarted at 0,
# so third-party explorers may report a different scheme.
if [ -z "$EPOCH" ]; then
  EPOCH=$(timeout 3s curl -s http://127.0.0.1:3085/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"{ bestChain(maxLength:1){ protocolState{ consensusState{ epoch } } } }"}' \
    | jq -r '.data.bestChain[0].protocolState.consensusState.epoch' 2>/dev/null)
fi

if [ -z "$EPOCH" ] || [ "$EPOCH" = "null" ]; then
  echo "Could not determine the epoch. Check the SSH tunnel on port 3085,"
  echo "or pass it explicitly: ./calc.sh N"
  exit 1
fi

source venv/bin/activate
python3 calc_rewards.py --epoch "$EPOCH" "$@"
