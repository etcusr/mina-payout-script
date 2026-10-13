import argparse
import getpass
import sys
from mina_client import MinaClient
import time
import yaml

with open("version", "r") as v_file:
    version = v_file.read()
print(f'Script version: {version}')

parser = argparse.ArgumentParser(description='Mina withdraw to cold wallet')
parser.add_argument('--amount', required=True, type=float,
                    help='amount in MINA to withdraw')
parser.add_argument('--to', metavar='B62q...',
                    help='destination address (default: WITHDRAW_TO_ADDRESS '
                         'from config.yml)')
args = parser.parse_args()

c = yaml.load(open('config.yml', encoding='utf8'), Loader=yaml.SafeLoader)
GRAPHQL_HOST      = str(c["GRAPHQL_HOST"])
GRAPHQL_PORT      = str(c["GRAPHQL_PORT"])
VALIDATOR_NAME    = str(c["VALIDATOR_NAME"])
default_fee       = int(c["DEFAULT_TX_FEE"])
send_from         = str(c["SEND_FROM_ADDRESS"])
TX_CHECK_TIMER    = int(c["TX_CHECK_TIMER_SECONDS"])

# Cold wallet - where the validator's commission is withdrawn to.
# Set WITHDRAW_TO_ADDRESS in config.yml, or pass --to on the command line.
TO_ADDRESS        = str(args.to or c.get("WITHDRAW_TO_ADDRESS") or "").strip()
if not TO_ADDRESS:
    exit("No destination address. Set WITHDRAW_TO_ADDRESS in config.yml "
         "or pass --to B62q...")

MEMO              = str(c.get("WITHDRAW_MEMO", ""))
DECIMAL           = 1e9
TIMEOUT           = 1
TX_LIST_TO_CHECK  = []
FAILED_PAYOUTS_FILE = "failed_withdraw.csv"
FAILED_PAYOUTS_LST  = []

amount_nanomina = int(args.amount * DECIMAL)
amount_mina = amount_nanomina / DECIMAL

WALLET_PASSWORD   = getpass.getpass(f'Wallet password for {send_from}: ')

graphql = MinaClient(graphql_host=GRAPHQL_HOST, graphql_port=GRAPHQL_PORT)

# Show the current balance for reference only - do NOT rely on it, there may
# be pending payouts that are not reflected yet
try:
    wallet = graphql.get_wallet(send_from)
    balance_nanomina = int(wallet["wallet"]["balance"]["total"])
    balance_mina = balance_nanomina / DECIMAL
    print(f'Current balance: {balance_mina} MINA (note: pending payouts not subtracted)')
except Exception as e:
    print(f'Could not fetch balance ({e}), continuing anyway')

print(f'Will send: {amount_mina} MINA --> {TO_ADDRESS}')
print(f'Tx fee: {default_fee / DECIMAL} MINA')

confirm = input('Confirm withdraw? [y/N]: ').strip().lower()
if confirm != 'y':
    exit('Aborted by user')

try:
    graphql.unlock_wallet(send_from, WALLET_PASSWORD)
except:
    print("Can't unlock wallet. Check password and address")


def send_transaction(to_address, amount_nanomina, from_address=send_from,  fee_nanomina=default_fee, memo=MEMO):
    if fee_nanomina > 1e9:
        exit(f"Tx fee is too high {fee_nanomina}")

    trans_res = graphql.send_payment(to_pk=to_address,
                                     from_pk=from_address,
                                     amount=amount_nanomina,
                                     fee=fee_nanomina,
                                     memo=memo)
    return trans_res


# Send to cold wallet
hash_result = send_transaction(
        to_address=TO_ADDRESS,
        amount_nanomina=amount_nanomina,
    )
TX_LIST_TO_CHECK.append(hash_result)
payment = hash_result["sendPayment"]["payment"]
print(f'Submitted, nonce {payment["nonce"]}: '
      f'https://minascan.io/mainnet/tx/{payment["id"]}')


try:
    graphql.lock_wallet(send_from)
except Exception as err:
    print(f"Can't lock wallet: {err}")

# Wait for inclusion in a block. Same loop as send_payout.py: a progress bar
# instead of a status line every second.
print(f'\nWaiting for the withdrawal to be included '
      f'(up to {TX_CHECK_TIMER / 60:.0f} min)')

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

wait_started = time.time()
deadline = time.time() + TX_CHECK_TIMER
bar = tqdm(total=len(TX_LIST_TO_CHECK), unit="tx", desc="  included", ncols=100,
           bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                      "[{elapsed}<{remaining}{postfix}]") if tqdm else None


def _say(msg):
    bar.write(msg) if bar else print(msg, flush=True)


def _hms(seconds):
    m, s = divmod(int(seconds), 60)
    return f'{m}m{s:02d}s' if m else f'{s}s'


errors = 0
try:
    while TX_LIST_TO_CHECK and time.time() < deadline:
        for tx in list(TX_LIST_TO_CHECK):
            tx_hash = tx["sendPayment"]["payment"]["id"]
            try:
                tx_data = str(graphql.get_transaction_status(tx_hash))
            except Exception as tx_status_err:
                errors += 1
                if errors in (1, 10, 100):      # don't paper the screen with these
                    _say(f"  can't read the tx status ({tx_status_err}), still trying")
                continue

            if "error" in tx_data:
                errors += 1
                continue

            if "INCLUDED" in tx_data or "included" in tx_data:
                TX_LIST_TO_CHECK.remove(tx)
                if bar:
                    bar.update(1)

        if TX_LIST_TO_CHECK:
            if bar:
                bar.set_postfix_str("pending", refresh=True)
            time.sleep(5)
except KeyboardInterrupt:
    # The payment is already in the mempool; stopping the wait does not cancel it.
    if bar:
        bar.close()
    print('\nStopped waiting. The withdrawal was already submitted and will '
          'still be included - do not resend it.')
    sys.exit(130)

if bar:
    bar.close()

if not TX_LIST_TO_CHECK:
    print(f'\nWithdrawal included in {_hms(time.time() - wait_started)}.')
    sys.exit(0)

# Timed out. It may yet land, so it is recorded rather than assumed lost.
print('\nTimed out with the withdrawal not yet included.')
for tx_ in TX_LIST_TO_CHECK:
    tx_ = tx_["sendPayment"]["payment"]
    amount_wei = tx_["amount"]
    FAILED_PAYOUTS_LST.append(
        f'{tx_["to"]};{amount_wei};{int(amount_wei) / 1e9};;')
with open(FAILED_PAYOUTS_FILE, "w") as failed_f:
    for t in FAILED_PAYOUTS_LST:
        failed_f.write(f'{t}\n')
print(f'Written to {FAILED_PAYOUTS_FILE}. It may still be included - check the '
      f'explorer before resending.')
sys.exit(1)
