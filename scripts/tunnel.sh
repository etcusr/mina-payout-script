#!/usr/bin/env bash
# Manual foreground: ./scripts/tunnel.sh [ssh-host]
# Background: ./scripts/tunnel.sh [ssh-host] --bg (or --ensure)
# Stop only this tool's owned SSH master: ./scripts/tunnel.sh --stop
# Host/ports come from config.yml or MINA_SSH_HOST and port environment vars.
set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
PROJECT_DIR="$(cd -P "$(dirname "$SOURCE")/.." && pwd)"
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
  source "$PROJECT_DIR/venv/bin/activate"
fi
exec python3 "$PROJECT_DIR/tunnel.py" "$@"
