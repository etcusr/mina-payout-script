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

try:
    queued = len(graphql.get_pooled_payments(send_from)["pooledUserCommands"])
    print(f'{queued} payment(s) queued in the mempool')
except Exception as err:
    print(f'Could not read the mempool: {err}')

# Wait for inclusion in a block. This is not the 17-hour wait to K=290 finality,
# just a block or two - but it matters: reconcile.py reads the chain, so a
# transaction still in the pool would be recorded as never paid.
print(f'\nWaiting for {len(TX_LIST_TO_CHECK)} payment(s) to be included '
      f'(up to {TX_CHECK_TIMER / 60:.0f} min)')

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

wait_started = time.time()
first_included = None
deadline = time.time() + TX_CHECK_TIMER
bar = tqdm(total=len(TX_LIST_TO_CHECK), unit="tx", desc="  included", ncols=100,
           bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                      "[{elapsed}<{remaining}{postfix}]") if tqdm else None


def _say(msg):
    bar.write(msg) if bar else print(msg, flush=True)


errors = 0
while TX_LIST_TO_CHECK and time.time() < deadline:
    for tx in list(TX_LIST_TO_CHECK):
        if time.time() >= deadline:
            break
        tx_hash = tx["sendPayment"]["payment"]["id"]
        try:
            tx_data = str(graphql.get_transaction_status(tx_hash))
        except Exception as tx_status_err:
            errors += 1
            if errors in (1, 10, 100):      # don't paper the screen with these
                _say(f"  can't read a tx status ({tx_status_err}), still trying")
            continue

        if "error" in tx_data:
            errors += 1
            continue

        if "INCLUDED" in tx_data or "included" in tx_data:
            TX_LIST_TO_CHECK.remove(tx)
            if first_included is None:
                first_included = time.time()
            if bar:
                bar.update(1)

    if TX_LIST_TO_CHECK:
        if bar:
            bar.set_postfix_str(f"{len(TX_LIST_TO_CHECK)} pending", refresh=True)
        time.sleep(5)

if bar:
    bar.close()

def _hms(seconds):
    m, s = divmod(int(seconds), 60)
    return f'{m}m{s:02d}s' if m else f'{s}s'


if not TX_LIST_TO_CHECK:
    total = time.time() - wait_started
    print(f'\nAll {len(rows)} payment(s) confirmed in {_hms(total)}.')
    if first_included:
        # The gap between the two is how long the batch took to drain once
        # blocks started coming; the first figure is just waiting for a block.
        print(f'  first included after {_hms(first_included - wait_started)}, '
              f'the rest within {_hms(time.time() - first_included)}')
    if os.path.exists('reconcile.py'):
        print(f'Next: python3 reconcile.py --epoch {EPOCH} --commit')
    sys.exit(0)

# Timed out with some still in the pool. They may yet land, so they are recorded
# for a retry rather than assumed lost - and reconcile.py will refuse to commit
# until the chain says one way or the other.
print(f'\nTimed out with {len(TX_LIST_TO_CHECK)} payment(s) not yet included.')
for tx_ in TX_LIST_TO_CHECK:
    tx_ = tx_["sendPayment"]["payment"]
    amount_wei = tx_["amount"]
    FAILED_PAYOUTS_LST.append(
        f'{tx_["to"]};{amount_wei};{int(amount_wei) / 1e9};;')
with open(FAILED_PAYOUTS_FILE, "w") as failed_f:
    for t in FAILED_PAYOUTS_LST:
        failed_f.write(f'{t}\n')
print(f'Written to {FAILED_PAYOUTS_FILE}. They may still be included - check '
      f'before resending:')
print(f'  python3 reconcile.py --epoch {EPOCH}')
sys.exit(1)
