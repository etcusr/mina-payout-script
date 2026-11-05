#!/usr/bin/env bash
# Usage: ./calc.sh [epoch] [calc_rewards.py flags...]
# Both explicit and automatically selected epochs require healthy services.
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
PROJECT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
cd "$PROJECT_DIR"
if [ -z "${VIRTUAL_ENV:-}" ] && [ -f venv/bin/activate ]; then
  source venv/bin/activate
fi

for _a in "$@"; do
  case "$_a" in --help|-h) exec python3 calc_rewards.py "$@" ;; esac
done

# This remains in calc.sh so already-sourced mina_calc/balance_mina functions
# immediately acquire autostart without shell startup file changes.
echo 'Туннель: проверяю подключение…' >&2
python3 tunnel.py --ensure >/dev/null
echo 'Туннель: OK' >&2

EPOCH=""
BARE_EPOCH=""
if [[ "${1:-}" =~ ^[0-9]+$ ]]; then
  EPOCH="$1"
  BARE_EPOCH="$1"
  shift
else
  _prev=""
  for _a in "$@"; do
    case "$_prev" in --epoch|-e) EPOCH="$_a" ;; esac
    case "$_a" in --epoch=*) EPOCH="${_a#--epoch=}" ;; esac
    _prev="$_a"
  done
fi
DOCTOR_ARGS=(doctor --print-epoch)
if [ -n "$EPOCH" ]; then DOCTOR_ARGS+=(--epoch "$EPOCH"); fi
echo 'Проверка валидатора и архива перед расчётом…' >&2
CURRENT_EPOCH="$(python3 payout_cli.py "${DOCTOR_ARGS[@]}")"
echo 'Проверка валидатора и архива: OK' >&2
if [ -n "$BARE_EPOCH" ]; then
  exec python3 calc_rewards.py --epoch "$BARE_EPOCH" "$@"
elif [ -n "$EPOCH" ]; then
  exec python3 calc_rewards.py "$@"
else
  exec python3 calc_rewards.py --epoch "$CURRENT_EPOCH" "$@"
fi
