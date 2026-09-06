import argparse
import getpass
import pprint
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

print(graphql.get_wallets())

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
print(hash_result)


print(f'Trying to lock wallet: {send_from}')
try:
    graphql.lock_wallet(send_from)
except:
    print("Can't lock wallet")

pool = graphql.get_pooled_payments(send_from)
print(len(pool["pooledUserCommands"]))
print(pool["pooledUserCommands"])

# Let's check all transactions status
print(f'Starting verification of sent transactions. Checker timer = {TX_CHECK_TIMER / 60} min')
while len(TX_LIST_TO_CHECK):
    tx_data = ""
    print(f'{len(TX_LIST_TO_CHECK)} pending txs in the pool. Timer timeout = {TX_CHECK_TIMER} sec')
    for n, tx in enumerate(TX_LIST_TO_CHECK, start=1):
        t1 = time.time()
        tx_hash = tx["sendPayment"]["payment"]["id"]
        try:
            tx_data = graphql.get_transaction_status(tx_hash)
        except Exception as tx_status_err:
            print(f'Can\'t get TX status: {tx_status_err}')
            TX_CHECK_TIMER -= time.time() - t1
            continue

        if "error" in str(tx_data):
            print(f'Can\'t get TX status {tx_data}')
            TX_CHECK_TIMER -= time.time() - t1
            continue

        elif "pending" in str(tx_data) or "PENDING" in str(tx_data):
            print(f'Tx has pending status: https://minaexplorer.com/payment/{tx_hash}')

        elif "INCLUDED" in str(tx_data) or "included" in str(tx_data):
            print(f'Transaction sent successfully: https://minaexplorer.com/payment/{tx_hash}')
            TX_LIST_TO_CHECK.remove(tx)

        else:
            print(f'Else triggered: {tx_data}')

        time.sleep(1)

        TX_CHECK_TIMER -= time.time() - t1
        if TX_CHECK_TIMER <= 0:
            print(f'Timeout: Transaction verification took too long. Save failed transactions and exit\n'
                  f'Unconfirmed transactions: {len(TX_LIST_TO_CHECK)}')
            for tx_ in TX_LIST_TO_CHECK:
                tx_ = tx_["sendPayment"]["payment"]
                to_addr = tx_["to"]
                amount_wei = tx_["amount"]
                amount_in_mina = int(amount_wei) / 1e9
                FAILED_PAYOUTS_LST.append(f'{to_addr};{str(amount_wei)};{str(amount_in_mina)};;')
            with open(FAILED_PAYOUTS_FILE, "w") as failed_f:
                for t in FAILED_PAYOUTS_LST:
                    failed_f.write(f'{t}\n')
            print(f'Check file with failed txs - {FAILED_PAYOUTS_FILE}')
            exit(1)

        if len(TX_LIST_TO_CHECK) == 0:
            print("All transactions are successfully confirmed!")
            exit(0)
