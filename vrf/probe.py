#!/usr/bin/env python3
"""Probe Mina's evaluateVrf to find which slots this validator wins.

The daemon evaluates the VRF per (delegator, slot) pair: if any delegator's
stake clears the threshold for a slot, we get to produce a block there. So
"do we win slot S" means checking every delegator for S - there is no shortcut.

evaluateVrf needs the block-producer private key unlocked in the daemon's
wallet. This script unlocks it, runs the scan, and locks it back - including
on Ctrl+C, on error, and on SIGTERM.

Usage:
    python3 vrf/probe.py --epoch 0              # whole epoch
    python3 vrf/probe.py --epoch 0 --from-now   # only slots still ahead
    python3 vrf/probe.py --epoch 1              # next epoch (if seed is known)
    python3 vrf/probe.py 619                    # one specific slot
    python3 vrf/probe.py 1235 7139              # explicit slot range

Slot numbers are global-slot-since-hard-fork, which is what evaluateVrf wants.
Epoch N therefore spans slots N*7140 .. (N+1)*7140-1.

Only the current epoch and the next one can be scanned: the VRF needs that
epoch's seed and staking ledger, and the daemon only publishes those two.

Note: every eval is an elliptic-curve op on the *producer* node, and Mina's
daemon is single-threaded (OCaml Async), so extra workers barely help - the
ceiling is roughly 650 evals/s no matter what. Workers mostly hide network
latency.
"""

import argparse
import atexit
import getpass
import glob
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, ".")
import yaml

c = yaml.safe_load(open("config.yml", encoding="utf8"))
GRAPHQL = f"http://{c['GRAPHQL_HOST']}:{c['GRAPHQL_PORT']}/graphql"
VALIDATOR = str(c["VALIDATOR_ADDRESS"])
SLOTS_PER_EPOCH = int(c.get("SLOTS_PER_EPOCH", 7140))
LEDGER_GLOB = "archive/cache/ledgers/epoch-*.json"

_print_lock = threading.Lock()
_unlocked = False


# --- daemon plumbing -------------------------------------------------------

def gql(query, timeout=600):
    req = urllib.request.Request(
        GRAPHQL,
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def unlock(pw):
    return gql('mutation { unlockAccount(input: {publicKey: "%s", password: %s}) '
               '{ account { publicKey locked } } }' % (VALIDATOR, json.dumps(pw)))


def lock():
    return gql('mutation { lockAccount(input: {publicKey: "%s"}) '
               '{ account { publicKey locked } } }' % VALIDATOR)


def ensure_locked():
    """Lock the producer key back. Safe to call repeatedly."""
    global _unlocked
    if not _unlocked:
        return
    _unlocked = False
    for _ in range(3):
        try:
            if not lock().get("errors"):
                print("producer key locked back", file=sys.stderr)
                return
        except Exception:
            time.sleep(1)
    print(f"WARNING: could not lock {VALIDATOR} - lock it manually via GraphQL",
          file=sys.stderr)


def _on_signal(signum, frame):
    ensure_locked()
    sys.exit(130)


# --- scanning --------------------------------------------------------------

def cached_path(epoch):
    return f"vrf/won_slots_epoch{int(epoch)}.json"


def load_cached(epoch, ledger_hash=None, seed=None, require_full=True):
    """Return a previous scan for this epoch, or None.

    A cached scan is only valid for the ledger hash and seed it was made with
    (both change every epoch), and by default only if it covered the whole
    epoch rather than a partial range.
    """
    path = cached_path(epoch)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            d = json.load(f)
    except Exception:
        return None
    if ledger_hash and d.get("ledger_hash") != ledger_hash:
        return None
    if seed and d.get("seed") != seed:
        return None
    if require_full:
        lo, hi = d.get("scanned_range", [None, None])
        want_lo = int(epoch) * SLOTS_PER_EPOCH
        want_hi = want_lo + SLOTS_PER_EPOCH - 1
        if lo != want_lo or hi != want_hi:
            return None
    return d


def scan_chunk(slots, delegators, seed, total_stake):
    """Evaluate every (slot, delegator) pair in one GraphQL request."""
    aliases, meta = [], {}
    for si, slot in enumerate(slots):
        for di, (idx, pk, bal, nano) in enumerate(delegators):
            key = f"s{si}d{di}"
            meta[key] = (slot, idx, pk, bal)
            aliases.append(
                f'{key}: evaluateVrf(publicKey: "{VALIDATOR}", '
                f'message: {{delegatorIndex: {idx}, epochSeed: "{seed}", '
                f'globalSlot: {slot}}}, '
                f'vrfThreshold: {{totalStake: "{total_stake}", '
                f'delegatedStake: "{nano}"}}) {{ thresholdMet }}'
            )
    res = gql("{\n" + "\n".join(aliases) + "\n}")
    if "errors" in res:
        raise RuntimeError(res["errors"][0].get("message", "unknown error"))
    return [meta[k] for k, v in (res.get("data") or {}).items()
            if v and v.get("thresholdMet")]


# --- reusable API (also used by calc_rewards.py --vrf) ----------------------

def epoch_context(epoch):
    """Seed / total stake / ledger hash for an epoch, from the daemon.

    Only the current epoch and the next one are available: those are the only
    two the daemon publishes (stakingEpochData / nextEpochData).
    """
    st = gql("""
        { bestChain(maxLength:1){ protocolState{ consensusState{
            epoch slot
            stakingEpochData{ seed ledger{ hash totalCurrency } }
            nextEpochData   { seed ledger{ hash totalCurrency } }
          } } } }
    """)
    cs = st["data"]["bestChain"][0]["protocolState"]["consensusState"]
    cur_epoch = int(cs["epoch"])
    cur_slot = int(cs["slot"])
    cur_global = cur_epoch * SLOTS_PER_EPOCH + cur_slot

    if epoch == cur_epoch:
        ed, which = cs["stakingEpochData"], "stakingEpochData"
    elif epoch == cur_epoch + 1:
        ed, which = cs["nextEpochData"], "nextEpochData"
    else:
        raise RuntimeError(
            f"epoch {epoch} is not scannable: the daemon is on epoch {cur_epoch} "
            f"and only publishes the seed for {cur_epoch} and {cur_epoch + 1}")

    return {
        "epoch": epoch,
        "which": which,
        "seed": ed["seed"],
        "total_stake": ed["ledger"]["totalCurrency"],
        "ledger_hash": ed["ledger"]["hash"],
        "cur_epoch": cur_epoch,
        "cur_slot": cur_slot,
        "cur_global": cur_global,
    }


def load_delegators(epoch, ledger_hash, ledger_path=None, verbose=True):
    """Delegators of our validator in that epoch's staking ledger.

    Returns [(ledger_index, pk, balance_mina, balance_nano), ...].
    The ledger is downloaded into the shared cache if missing.
    """
    if ledger_path:
        if verbose:
            print(f"loading {ledger_path} ...", flush=True)
        ledger = json.load(open(ledger_path))
    else:
        cands = [p for p in glob.glob(LEDGER_GLOB) if ledger_hash in p]
        if cands:
            if verbose:
                print(f"loading {cands[0]} ...", flush=True)
            ledger = json.load(open(cands[0]))
        else:
            # Not cached - pull it through the same cache calc_rewards.py uses
            # so we never keep two 280 MB copies around.
            if verbose:
                print(f"staking ledger for epoch {epoch} not cached, "
                      f"downloading (~280 MB, once per epoch)...", flush=True)
            import GraphQL
            ledger = GraphQL.ensureStakingLedger(epoch, ledger_hash)

    return [
        (i, a["pk"], float(a["balance"]), int(round(float(a["balance"]) * 1e9)))
        for i, a in enumerate(ledger)
        if a.get("delegate") == VALIDATOR and float(a.get("balance", 0)) > 0
    ]


def scan_range(lo, hi, ctx, delegators, workers=8, batch=20, password=None,
               verbose=True):
    """Evaluate the VRF over [lo, hi] and return the result dict (also saved)."""
    global _unlocked

    seed, total_stake = ctx["seed"], ctx["total_stake"]
    epoch = ctx["epoch"]
    slots = list(range(lo, hi + 1))
    chunks = [slots[i:i + batch] for i in range(0, len(slots), batch)]
    total_evals = len(slots) * len(delegators)

    if verbose:
        print(f"{len(slots)} slots x {len(delegators)} delegators = "
              f"{total_evals:,} evaluations in {len(chunks)} requests, "
              f"{workers} workers")
        print(f"expect roughly {total_evals/650/60:.1f} min "
              f"(daemon ceiling is ~650 evals/s)\n")

    if password is None:
        password = getpass.getpass("BP key password (Enter if none): ")
    r = unlock(password)
    if "errors" in r:
        raise RuntimeError(f"unlock failed: {r['errors'][0]['message']}")
    _unlocked = True
    atexit.register(ensure_locked)
    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except ValueError:
        pass          # not on the main thread - handlers are optional
    if verbose:
        print("producer key unlocked\n")

    winners, done, t0 = [], 0, time.time()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(scan_chunk, ch, delegators, seed, total_stake): ch
                       for ch in chunks}
            for fut in as_completed(futures):
                ch = futures[fut]
                try:
                    found = fut.result()
                except Exception as e:
                    with _print_lock:
                        print(f"\n  chunk {ch[0]}..{ch[-1]} FAILED: {e}", flush=True)
                    continue
                done += 1
                winners.extend(found)
                if verbose:
                    with _print_lock:
                        for slot, idx, pk, bal in sorted(found):
                            print(f"\r  >>> WON slot {slot:>6}  idx {idx} "
                                  f"({bal:,.2f} MINA)  {pk}", flush=True)
                        el = time.time() - t0
                        rate = done / el if el else 0
                        eta = (len(chunks) - done) / rate if rate else 0
                        print(f"  [{100*done/len(chunks):5.1f}%] {done}/{len(chunks)} "
                              f"requests, {el:.0f}s elapsed, ~{eta:.0f}s left, "
                              f"{rate*batch*len(delegators):,.0f} evals/s",
                              end="\r", flush=True)
    finally:
        if verbose:
            print()
        ensure_locked()

    result = {
        "epoch": epoch,
        "ledger_hash": ctx["ledger_hash"],
        "seed": seed,
        "scanned_range": [lo, hi],
        "scanned_at_global_slot": ctx["cur_global"],
        "slots": [{"global_slot": s, "epoch_slot": s % SLOTS_PER_EPOCH,
                   "delegator_index": i, "pk": p, "balance": b}
                  for s, i, p, b in sorted(winners)],
    }
    os.makedirs("vrf", exist_ok=True)
    with open(cached_path(epoch), "w") as f:
        json.dump(result, f, indent=2)
    if verbose:
        print(f"took {time.time()-t0:.0f}s, saved to {cached_path(epoch)}")
    return result


def scan_epoch(epoch, workers=8, batch=20, password=None, verbose=True):
    """Scan a whole epoch. Convenience wrapper used by calc_rewards.py --vrf."""
    ctx = epoch_context(int(epoch))
    delegators = load_delegators(epoch, ctx["ledger_hash"], verbose=verbose)
    if not delegators:
        raise RuntimeError("no delegators with non-zero stake in this ledger")
    lo = int(epoch) * SLOTS_PER_EPOCH
    hi = lo + SLOTS_PER_EPOCH - 1
    return scan_range(lo, hi, ctx, delegators, workers=workers, batch=batch,
                      password=password, verbose=verbose)


# --- CLI -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description="Find VRF-won slots for this validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Epoch N spans global slots N*%d .. (N+1)*%d-1"
               % (SLOTS_PER_EPOCH, SLOTS_PER_EPOCH),
    )
    ap.add_argument("slot_from", type=int, nargs="?",
                    help="first global slot (omit when using --epoch)")
    ap.add_argument("slot_to", type=int, nargs="?",
                    help="last global slot, inclusive")
    ap.add_argument("-e", "--epoch", type=int,
                    help="scan a whole epoch instead of a slot range")
    ap.add_argument("--from-now", action="store_true",
                    help="with --epoch, skip slots that already passed")
    ap.add_argument("-w", "--workers", type=int, default=8,
                    help="parallel GraphQL requests (default 8)")
    ap.add_argument("-b", "--batch", type=int, default=20,
                    help="slots per request (default 20)")
    ap.add_argument("--ledger", help="staking ledger JSON (default: picked by hash)")
    args = ap.parse_args()

    if args.epoch is not None:
        if args.slot_from is not None:
            ap.error("use either --epoch or an explicit slot range, not both")
        epoch = args.epoch
    else:
        if args.slot_from is None:
            ap.error("give --epoch N, or a slot / slot range")
        epoch = args.slot_from // SLOTS_PER_EPOCH
        hi_epoch = (args.slot_to if args.slot_to is not None
                    else args.slot_from) // SLOTS_PER_EPOCH
        if hi_epoch != epoch:
            sys.exit(f"range spans epochs {epoch} and {hi_epoch} - the seed "
                     f"differs between them, scan them separately")

    try:
        ctx = epoch_context(epoch)
    except Exception as e:
        sys.exit(str(e))

    if args.epoch is not None:
        lo = epoch * SLOTS_PER_EPOCH
        hi = lo + SLOTS_PER_EPOCH - 1
        if args.from_now:
            lo = max(lo, ctx["cur_global"] + 1)
            if lo > hi:
                sys.exit(f"epoch {epoch} is already over "
                         f"(current slot {ctx['cur_global']})")
    else:
        lo = args.slot_from
        hi = args.slot_to if args.slot_to is not None else args.slot_from

    print(f"daemon at epoch {ctx['cur_epoch']} slot {ctx['cur_slot']} "
          f"(global {ctx['cur_global']})")
    print(f"scanning epoch {epoch}, global slots {lo}..{hi}  [{ctx['which']}]")
    print(f"seed  {ctx['seed']}")
    print(f"stake {int(ctx['total_stake'])/1e9:,.2f} MINA total\n")

    try:
        delegators = load_delegators(epoch, ctx["ledger_hash"], args.ledger)
    except Exception as e:
        sys.exit(f"could not load the staking ledger: {e}")
    if not delegators:
        sys.exit("no delegators with non-zero stake in this ledger")
    pool_stake = sum(d[2] for d in delegators)
    print(f"{len(delegators)} delegators, {pool_stake:,.2f} MINA in the pool "
          f"({100*pool_stake/(int(ctx['total_stake'])/1e9):.4f}% of network)")

    res = scan_range(lo, hi, ctx, delegators,
                     workers=args.workers, batch=args.batch)

    print(f"\n=== {len(res['slots'])} winning slot(s) in epoch {epoch}, "
          f"slots {lo}..{hi} ===")
    for s in res["slots"]:
        when = "past" if s["global_slot"] <= ctx["cur_global"] else "ahead"
        print(f"  slot {s['global_slot']:>6} (epoch slot {s['epoch_slot']:>4}, "
              f"{when:>5})  idx {s['delegator_index']:>6}  "
              f"{s['balance']:>14,.2f} MINA  {s['pk']}")


if __name__ == "__main__":
    main()
