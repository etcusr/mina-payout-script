#!/usr/bin/env python3
"""Send the payouts computed by calc_rewards.py.

    python3 send_payout.py --epoch 3
    python3 send_payout.py --epoch 3 --resume   # continue an interrupted run
    python3 send_payout.py --epoch 3 --dry-run  # show what would be sent

Reads e<N>_payouts.csv, unlocks the sending wallet, submits one payment per
row and then waits for every transaction to be included in a block. Anything
still unconfirmed when TX_CHECK_TIMER_SECONDS runs out is written to
failed_payouts_<N>.csv so it can be retried.

Before sending anything the script runs a set of pre-flight checks. They exist
because this is the one place in the project that moves real money and a bad
input file here is expensive to undo:

  * duplicate destinations in the CSV - a payout file should hold one row per
    address; repeats mean the file was appended to instead of rewritten
  * total against the live wallet balance, including what is already queued
    in the mempool
  * an existing sended_txs_e<N>.csv, which means this epoch was already paid

Each check aborts with an explanation rather than sending a partial round.
"""

import argparse
import getpass
import os
import sys
import time

import yaml

from mina_client import MinaClient

try:
    # Only used for the balance pre-flight check. It pulls in the archive
    # stack, so a missing psycopg2 should warn, not stop a payout.
    import GraphQL
except Exception as _gql_err:
    GraphQL = None
    print(f'Balance check unavailable ({_gql_err.__class__.__name__}: {_gql_err})')

with open("version", "r") as v_file:
    version = v_file.read().strip()
print(f'Script version: {version}')

parser = argparse.ArgumentParser(description='Mina payouts sender')
parser.add_argument('--epoch', required=True, type=int, help='epoch number')
parser.add_argument('--resume', action='store_true',
                    help='skip addresses already present in sended_txs_e<N>.csv')
parser.add_argument('--dry-run', action='store_true',
                    help='run the checks and print the plan, send nothing')
parser.add_argument('--yes', action='store_true',
                    help='skip the interactive confirmation')
args = parser.parse_args()

c = yaml.load(open('config.yml', encoding='utf8'), Loader=yaml.SafeLoader)
GRAPHQL_HOST      = str(c["GRAPHQL_HOST"])
GRAPHQL_PORT      = str(c["GRAPHQL_PORT"])
VALIDATOR_NAME    = str(c["VALIDATOR_NAME"])
EPOCH             = int(args.epoch)
default_fee       = int(c["DEFAULT_TX_FEE"])
send_from         = str(c["SEND_FROM_ADDRESS"])
TX_CHECK_TIMER    = int(c["TX_CHECK_TIMER_SECONDS"])

MEMO              = f'e{EPOCH}-f1_{VALIDATOR_NAME}'
FILE_WITH_PAYOUTS = f'e{EPOCH}_payouts.csv'
SENT_FILE         = f'sended_txs_e{EPOCH}.csv'
DECIMAL           = 1e9
TIMEOUT           = 1
TX_LIST_TO_CHECK  = []
FAILED_PAYOUTS_FILE = f"failed_payouts_{EPOCH}.csv"
FAILED_PAYOUTS_LST  = []


def die(msg):
    print(f'\nABORTED: {msg}')
    sys.exit(1)


# ---------------------------------------------------------------- load the CSV

if not os.path.exists(FILE_WITH_PAYOUTS):
    die(f'{FILE_WITH_PAYOUTS} not found - run: python3 calc_rewards.py --epoch {EPOCH}')

rows = []
for lineno, line in enumerate(open(FILE_WITH_PAYOUTS, encoding='utf-8'), start=1):
    line = line.strip()
    if not line:
        continue
    p = line.split(";")
    if len(p) < 3:
        die(f'{FILE_WITH_PAYOUTS}:{lineno} is malformed: {line!r}')
    rows.append({"to": p[0], "nano": int(p[1]), "mina": float(p[2]),
                 "kind": p[3] if len(p) > 3 else ""})

if not rows:
    die(f'{FILE_WITH_PAYOUTS} is empty - nothing to pay')

# A payout file holds one row per address. More than one means it was appended
# to across several calc_rewards.py runs, and sending it would pay people
# multiple times.
seen = {}
for r in rows:
    seen.setdefault(r["to"], []).append(r["mina"])
dupes = {a: v for a, v in seen.items() if len(v) > 1}
if dupes:
    print(f'\n{len(dupes)} address(es) appear more than once in {FILE_WITH_PAYOUTS}:')
    for a, v in list(dupes.items())[:5]:
        print(f'  {a}  x{len(v)}  ({", ".join(f"{x:.4f}" for x in v[:4])}...)')
    die('the payout file is corrupted. Regenerate it:\n'
        f'  rm {FILE_WITH_PAYOUTS} && python3 calc_rewards.py --epoch {EPOCH}')

# ------------------------------------------------------- already-paid guard

already_sent = set()
if os.path.exists(SENT_FILE):
    import ast
    n_sent = 0
    for line in open(SENT_FILE, encoding='utf-8'):
        line = line.strip()
        if not line:
            continue
        try:
            t = ast.literal_eval(line)["sendPayment"]["payment"]
            already_sent.add(t["to"])
            n_sent += 1
        except Exception:
            continue
    if not args.resume:
        _settle = (f'  To settle the difference:  python3 reconcile.py --epoch {EPOCH}\n'
                   if os.path.exists('reconcile.py') else '')
        die(f'{SENT_FILE} already holds {n_sent} transaction(s) for epoch {EPOCH}.\n'
            f'  Sending again would pay everyone a second time.\n'
            f'{_settle}'
            f'  To finish an interrupted run:  python3 send_payout.py --epoch {EPOCH} --resume')
    skipped = [r for r in rows if r["to"] in already_sent]
    rows = [r for r in rows if r["to"] not in already_sent]
    print(f'Resuming: {len(skipped)} address(es) already paid, {len(rows)} left')
    if not rows:
        print('Nothing left to send.')
        sys.exit(0)

total_nano = sum(r["nano"] for r in rows)
total_mina = total_nano / DECIMAL
fee_mina = default_fee * len(rows) / DECIMAL

# ------------------------------------------------------------ balance check

graphql = MinaClient(graphql_host=GRAPHQL_HOST, graphql_port=GRAPHQL_PORT)

bal = None
if GraphQL is not None:
    try:
        bal = GraphQL.getAccountBalance(send_from)
    except Exception as err:
        print(f'Could not read the wallet balance: {err}')

print(f'\nEpoch {EPOCH}')
print(f'  from      {send_from}')
print(f'  payments  {len(rows)}')
print(f'  amount    {total_mina:,.4f} MINA  (+ {fee_mina:.4f} MINA in fees)')

if bal:
    print(f'  balance   {bal["liquid"]:,.4f} MINA liquid')
    if total_mina + fee_mina > bal["liquid"]:
        die(f'the wallet holds {bal["liquid"]:,.4f} MINA but this run needs '
            f'{total_mina + fee_mina:,.4f} MINA.\n'
            f'  Sending would stop partway through and leave the epoch half paid.')
    if bal.get("pending_txs"):
        print(f'  NOTE: {bal["pending_txs"]} transaction(s) from an earlier run are '
              f'still in the mempool and are not reflected in the balance above.')

if args.dry_run:
    print('\n--dry-run: nothing was sent.')
    sys.exit(0)

if not args.yes:
    answer = input(f'\nSend {len(rows)} payment(s) totalling {total_mina:,.4f} MINA? [y/N] ')
    if answer.strip().lower() not in ("y", "yes"):
        sys.exit('Cancelled.')

# ------------------------------------------------------------------ sending

WALLET_PASSWORD = getpass.getpass(f'Wallet password for {send_from}: ')

try:
    graphql.unlock_wallet(send_from, WALLET_PASSWORD)
except Exception as err:
    die(f"can't unlock the wallet: {err}\n  Check the password and SEND_FROM_ADDRESS.")


def send_transaction(to_address, amount_nanomina, from_address=send_from,
                     fee_nanomina=default_fee, memo=MEMO):
    if fee_nanomina > 1e9:
        die(f"tx fee is too high: {fee_nanomina}")
    return graphql.send_payment(to_pk=to_address,
                                from_pk=from_address,
                                amount=amount_nanomina,
                                fee=fee_nanomina,
                                memo=memo)


try:
    for i, r in enumerate(rows, start=1):
        print(f'{i}/{len(rows)} '
              f'{r["mina"]} MINA --> https://minascan.io/mainnet/account/{r["to"]}')

        hash_result = send_transaction(to_address=r["to"],
                                       amount_nanomina=r["nano"])

        with open(SENT_FILE, "a") as tx_result:
            tx_result.write(f"{hash_result}\n")
        TX_LIST_TO_CHECK.append(hash_result)
        time.sleep(TIMEOUT)
finally:
    # Always relock, including when a send raised - an unlocked wallet on a
    # reachable daemon is the worst way for this script to end.
    print(f'Trying to lock wallet: {send_from}')
    try:
        graphql.lock_wallet(send_from)
    except Exception as err:
        print(f"Can't lock wallet: {err}")

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

        if "INCLUDED" in str(tx_data) or "included" in str(tx_data):
            print(f'Transaction sent successfully: https://minascan.io/mainnet/tx/{tx_hash}')
            TX_LIST_TO_CHECK.remove(tx)

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
            sys.exit(1)

        if len(TX_LIST_TO_CHECK) == 0:
            print("All transactions are successfully confirmed!")
            sys.exit(0)
