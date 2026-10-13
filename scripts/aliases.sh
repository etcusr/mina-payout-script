# Shell aliases for the Mina payout scripts. Works in bash and zsh.
#
# Install:
#   echo "source /path/to/mina-payout-script/scripts/aliases.sh" >> ~/.zshrc
#
# Optional - lets `mina_tunnel` run without arguments:
#   export MINA_SSH_HOST=my-node

# Resolve the project directory from this file, following symlinks, so the
# aliases keep working wherever the project is moved.
if [ -n "${BASH_SOURCE[0]:-}" ]; then
    _mina_src="${BASH_SOURCE[0]}"
else
    _mina_src="${(%):-%N}"          # zsh
fi
while [ -L "$_mina_src" ]; do
    _mina_dir="$(cd -P "$(dirname "$_mina_src")" && pwd)"
    _mina_src="$(readlink "$_mina_src")"
    case "$_mina_src" in /*) ;; *) _mina_src="$_mina_dir/$_mina_src" ;; esac
done
export MINA_PAYOUT_DIR="$(cd -P "$(dirname "$_mina_src")/.." && pwd)"
unset _mina_src _mina_dir

# These are functions, not aliases, on purpose. A payout round is several
# commands against the same working directory, and the scripts read and write
# files relative to it. A command that runs the project in a subshell would
# leave you wherever you started, so the next one fails or - worse - reads a
# different directory's files. Each command below lands you in the project with
# the venv active, so whatever you run next just works.

# Enter the project and activate the venv. Every command below starts with this.
mina_cd() {
    [ -d "$MINA_PAYOUT_DIR" ] || {
        echo "MINA_PAYOUT_DIR is not set or missing: ${MINA_PAYOUT_DIR:-unset}" >&2
        return 1
    }
    cd "$MINA_PAYOUT_DIR" || return 1
    if [ -z "${VIRTUAL_ENV:-}" ] && [ -f venv/bin/activate ]; then
        . venv/bin/activate
    fi
}

# Calculate rewards. Takes an optional epoch, then any calc_rewards.py flag.
#   mina_calc              current epoch
#   mina_calc 3            epoch 3
#   mina_calc 3 --vrf      epoch 3 + VRF slot scan
mina_calc() { mina_cd && ./calc.sh "$@"; }

# Send the payouts for an epoch (asks for the wallet password).
#   mina_pay --epoch 3
mina_pay() { mina_cd && python3 send_payout.py "$@"; }

# Compare an epoch against what reached the chain, and carry the difference.
#   mina_reconcile --epoch 3 --commit
mina_reconcile() {
    mina_cd || return 1
    [ -f reconcile.py ] || { echo "reconcile.py is not installed here" >&2; return 1; }
    python3 reconcile.py "$@"
}

# Withdraw commission to the cold wallet.
#   mina_withdraw --amount 252.5
mina_withdraw() { mina_cd && python3 withdraw.py "$@"; }

# Predict which slots this validator wins in an epoch.
#   mina_vrf --epoch 3
mina_vrf() { mina_cd && python3 vrf/probe.py "$@"; }

# SSH tunnel to the node: GraphQL 3085 + Postgres 5432.
#   mina_tunnel my-node --bg
#   mina_tunnel --stop
mina_tunnel() { "$MINA_PAYOUT_DIR/scripts/tunnel.sh" "$@"; }
