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

# Calculate rewards. Takes an optional epoch, then any calc_rewards.py flag.
#   mina_calc              current epoch
#   mina_calc 3            epoch 3
#   mina_calc 3 --vrf      epoch 3 + VRF slot scan
alias mina_calc="\"\$MINA_PAYOUT_DIR/calc.sh\""

# Send the payouts for an epoch (asks for the wallet password).
#   mina_pay --epoch 3
alias mina_pay="cd \"\$MINA_PAYOUT_DIR\" && source venv/bin/activate && python3 send_payout.py"

# Withdraw commission to the cold wallet.
#   mina_withdraw --amount 252.5
alias mina_withdraw="cd \"\$MINA_PAYOUT_DIR\" && source venv/bin/activate && python3 withdraw.py"

# Predict which slots this validator wins in an epoch.
#   mina_vrf --epoch 3
alias mina_vrf="cd \"\$MINA_PAYOUT_DIR\" && source venv/bin/activate && python3 vrf/probe.py"

# SSH tunnel to the node: GraphQL 3085 + Postgres 5432.
#   mina_tunnel my-node --bg
#   mina_tunnel --stop
alias mina_tunnel="\"\$MINA_PAYOUT_DIR/scripts/tunnel.sh\""

# Jump into the project with the venv active.
alias mina_cd="cd \"\$MINA_PAYOUT_DIR\" && source venv/bin/activate"
