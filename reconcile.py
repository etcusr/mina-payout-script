#!/usr/bin/env python3
"""Compare what an epoch owed against what was actually sent, and carry the
difference into the ledger so the next epoch corrects it.

    python3 reconcile.py --epoch 0            # show the comparison
    python3 reconcile.py --epoch 0 --commit   # and write it to payout_ledger.json

Inputs:
    e<N>_payouts.csv      what the epoch should have paid (regenerate first!)
    sended_txs_e<N>.csv   what send_payout.py actually submitted

A positive delta means the address was overpaid and will receive that much less
next epoch; a negative delta means it was underpaid and gets topped up.

Run `calc_rewards.py --epoch N` immediately before this, so the payout file
reflects the final block count for the epoch. Reconciling against a stale file
carries the wrong numbers forward.
"""

import argparse
import ast
import collections
import json
import os
import sys

import yaml

import ledger


def read_owed(epoch):
    path = f"e{epoch}_payouts.csv"
    if not os.path.exists(path):
        sys.exit(f"{path} not found - run: python3 calc_rewards.py --epoch {epoch}")
    owed = {}
    for line in open(path, encoding="utf-8"):
        p = line.strip().split(";")
        if len(p) >= 3:
            owed[p[0]] = float(p[2])
    return owed


def read_sent(epoch):
    path = f"sended_txs_e{epoch}.csv"
    if not os.path.exists(path):
        return collections.Counter(), 0
    sent, n = collections.Counter(), 0
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            t = ast.literal_eval(line)["sendPayment"]["payment"]
            sent[t["to"]] += int(t["amount"]) / 1e9
            n += 1
        except Exception:
            continue
    return sent, n


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-e", "--epoch", type=int, required=True)
    ap.add_argument("--commit", action="store_true",
                    help="write the deltas into payout_ledger.json")
    ap.add_argument("--force", action="store_true",
                    help="commit again for an epoch already in the ledger history")
    args = ap.parse_args()

    cfg = yaml.safe_load(open("config.yml", encoding="utf8"))
    mine = {str(cfg.get("SEND_FROM_ADDRESS", "")),
            str(cfg.get("WITHDRAW_TO_ADDRESS", "")),
            str(cfg.get("VALIDATOR_ADDRESS", ""))}

    owed = read_owed(args.epoch)
    sent, n_tx = read_sent(args.epoch)

    print(f"epoch {args.epoch}: {n_tx} transaction(s) submitted, "
          f"{len(owed)} address(es) in the payout file\n")
    print(f"{'address':<56}{'sent':>11}{'owed':>11}{'delta':>11}")

    over = under = ext = 0.0
    deltas = {}
    for a in sorted(set(owed) | set(sent),
                    key=lambda x: -(sent.get(x, 0) - owed.get(x, 0))):
        s, o = sent.get(a, 0.0), owed.get(a, 0.0)
        d = s - o
        if abs(d) < 1e-6:
            continue
        deltas[a] = d
        if d > 0:
            over += d
            if a not in mine:
                ext += d
        else:
            under += -d
        tag = "  (mine)" if a in mine else ""
        print(f"{a:<56}{s:>11.4f}{o:>11.4f}{d:>+11.4f}{tag}")

    print(f"\nsent {sum(sent.values()):,.4f} | owed {sum(owed.values()):,.4f}")
    print(f"overpaid {over:,.4f} | underpaid {under:,.4f}")
    print(f"of the overpayment, {ext:,.4f} MINA sits on third-party addresses")

    if not args.commit:
        print("\nnothing written - re-run with --commit to store these in the ledger")
        return

    data = ledger.load()

    # Reconciling the same epoch twice would write its deltas into the ledger a
    # second time, doubling the correction. The history is the record of what
    # has already been accounted for.
    prior = [h for h in data.get("history", []) if h.get("epoch") == args.epoch]
    if prior and not args.force:
        print(f"\nepoch {args.epoch} was already reconciled on {prior[-1]['at']}:")
        print(f"  {prior[-1]['note']}")
        print("nothing written - its deltas are already in the ledger.")
        print("If the payout file has since changed and you really mean to apply")
        print("the difference again, re-run with --force.")
        return

    for a, d in deltas.items():
        # A positive delta means they hold our money, so their balance goes
        # negative and the next payout is reduced by that much.
        ledger.apply_delta(a, -d, data)
    ledger.record(data, args.epoch,
                  f"reconciled epoch {args.epoch}: "
                  f"overpaid {over:.4f}, underpaid {under:.4f}")

    print(f"\nwritten to {ledger.LEDGER_FILE}: {len(deltas)} address(es)")
    print("the next calc_rewards.py run will net these off automatically")


if __name__ == "__main__":
    main()
