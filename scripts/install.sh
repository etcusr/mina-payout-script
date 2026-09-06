#!/usr/bin/env bash
# One-shot setup: virtualenv, dependencies, config skeleton.
#
#   ./scripts/install.sh
#
# Safe to re-run: an existing config.yml is never overwritten.

set -euo pipefail

PROJECT_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

echo "==> project: $PROJECT_DIR"

# --- python ----------------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 not found. Install Python 3.9+ and re-run." >&2
  exit 1
fi
PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
echo "==> python $PY_VER"

if [ -d venv ]; then
  # A venv bakes in absolute paths, so one copied from elsewhere is broken.
  if ! ./venv/bin/python3 -c '' 2>/dev/null; then
    echo "==> existing venv is broken (moved project?), recreating"
    rm -rf venv
  fi
fi
[ -d venv ] || { echo "==> creating venv"; python3 -m venv venv; }

# shellcheck disable=SC1091
source venv/bin/activate
echo "==> installing dependencies"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# --- config ----------------------------------------------------------------
if [ -f config.yml ]; then
  echo "==> config.yml already exists, leaving it alone"
else
  cp config.example.yml config.yml
  echo "==> created config.yml from the example - edit it before running anything"
fi

# --- external tools --------------------------------------------------------
command -v jq   >/dev/null 2>&1 || echo "note: jq not found (calc.sh uses it; brew/apt install jq)"
command -v psql >/dev/null 2>&1 || echo "note: psql not found (optional, only for manual DB queries)"

cat <<EOF

Setup complete.

Next:
  1. Edit config.yml           - validator address, payout wallet, fees
  2. Open a tunnel to the node - ./scripts/tunnel.sh <ssh-host> --bg
  3. Try it                    - ./calc.sh --vrf

Optional shell aliases:
  echo "source $PROJECT_DIR/scripts/aliases.sh" >> ~/.bashrc   # or ~/.zshrc
EOF
