"""Carry-forward ledger: per-address balance between the pool and delegators.

Payouts do not always land exactly on the computed amount. A run can be
interrupted, a transaction can fail, an epoch can be recalculated after more
blocks matured, or - as happened here once - a corrupted payout file can send
people several times what they were owed.

Instead of chasing each of those by hand, the difference is carried into the
next epoch:

    balance > 0   the pool still owes this address that much
    balance < 0   this address received that much too much, and it is
                  deducted from their next payout

`reconcile.py` writes into this ledger after a payout round; `calc_rewards.py`
reads it and adjusts the amounts it writes to the payout CSV.

The file is plain JSON so it can be inspected and edited by hand:

    {
      "balances": {"B62q...": -357.5519, "B62q...": 0.14},
      "history":  [{"epoch": 0, "at": "...", "note": "..."}]
    }
"""

import json
import os
from datetime import datetime, timezone

LEDGER_FILE = "payout_ledger.json"


def load(path=LEDGER_FILE):
    if not os.path.exists(path):
        return {"balances": {}, "history": []}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("balances", {})
        d.setdefault("history", [])
        return d
    except Exception:
        return {"balances": {}, "history": []}


def save(data, path=LEDGER_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)


def balances(path=LEDGER_FILE):
    """{address: balance_mina}, skipping anything that rounds to zero."""
    return {a: b for a, b in load(path)["balances"].items() if abs(b) >= 1e-9}


def apply_delta(address, delta_mina, data):
    """Add `delta_mina` to an address' balance, in place."""
    cur = float(data["balances"].get(address, 0.0))
    new = cur + float(delta_mina)
    if abs(new) < 1e-9:
        data["balances"].pop(address, None)
    else:
        data["balances"][address] = round(new, 9)
    return new


def record(data, epoch, note, path=LEDGER_FILE):
    data["history"].append({
        "epoch": int(epoch),
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": note,
    })
    save(data, path)


def adjust(owed_mina, address, path=None, _cache={}):
    """Return this epoch's payout for `address` after applying its balance.

    Overpaid delegators get less (possibly nothing) until the debt is cleared;
    underpaid ones get the shortfall added on top.
    """
    key = path or LEDGER_FILE
    if key not in _cache:
        _cache[key] = balances(key)
    carry = _cache[key].get(address, 0.0)
    return max(0.0, owed_mina + carry), carry
