#!/bin/bash
# Compatible entrypoint for the existing guardian container loop.
set -eu
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/blocks_guardian.py" "$@"
