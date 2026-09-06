"""
GraphQL.py — SQL-backed replacement for the original MinaExplorer queries.

Public surface (same as before, drop-in for calc_rewards.py):

    getLatestHeight()      -> {"data": {"blocks": [{"blockHeight": int}]}}
    getLedgerHash(epoch)   -> str
    getStakingLedger(vars) -> {"data": {"stakes": [...]}}
    getBlocks(vars)        -> {"data": {"blocks": [...]}}

Backing data sources:
    - Local mina-archive Postgres (via SSH tunnel: 127.0.0.1:5432)
        * canonical blocks + transactions + internal_commands
    - Local Mina daemon GraphQL (via SSH tunnel: 127.0.0.1:3085)
        * current epoch's staking + next ledger hashes
    - gs://mina-staking-ledgers (public bucket, no auth)
        * staking ledger JSON dumps per epoch (~286 MB each)

Config keys consumed (config.yml):
    GRAPHQL_HOST          host of mina daemon GraphQL (127.0.0.1)
    GRAPHQL_PORT          port (3085)
    ARCHIVE_DB_URL        postgresql://user:pass@127.0.0.1:5432/archive
    SLOTS_PER_EPOCH       optional, default 7140
"""

import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path

import psycopg2
import psycopg2.extras
import requests
import yaml

try:
    from tqdm import tqdm
    _HAS_TQDM = True
except ImportError:
    _HAS_TQDM = False


# --- config + paths --------------------------------------------------------

_PROJECT_DIR = Path(__file__).resolve().parent
_CONFIG_PATH = _PROJECT_DIR / "config.yml"
_CACHE_DIR = _PROJECT_DIR / "archive" / "cache"
_LEDGERS_CACHE = _CACHE_DIR / "ledgers"

GCS_LEDGERS_URL = "https://storage.googleapis.com/mina-staking-ledgers"
GCS_LISTING = "https://storage.googleapis.com/storage/v1/b"

NANOMINA = 1_000_000_000

_session = requests.Session()
_session.headers.update({"User-Agent": "mina-payout-script/0.3.0"})

_cfg_cache = None

# Remember (ledger_hash -> epoch) for every getLedgerHash() call so that
# getStakingLedger() can resolve the epoch back from the hash without asking
# the daemon (which only knows the current/next epoch).
_hash_to_epoch = {}


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _config():
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf8"))
    return _cfg_cache


def _slots_per_epoch():
    return int(_config().get("SLOTS_PER_EPOCH", 7140))


# --- protocol eras ---------------------------------------------------------
#
# Mesa (2026-09-03 18:00 UTC) reset both global_slot_since_hard_fork and the
# epoch numbering back to 0. That makes slot_hf/7140 ambiguous on its own:
# Berkeley epoch 5 and Mesa epoch 5 produce the same value.
#
# Eras are told apart via protocol_version_id in the blocks table:
#     1 = pre-Berkeley mainnet   (heights 1..359604)
#     2 = Berkeley               (heights 359605..548187)
#     3 = Mesa                   (heights 548147..)
#
# Defaults to the current (highest) era. Set PROTOCOL_VERSION_ID in config.yml
# to recalculate a historical epoch from an older era.

_pv_cache = None


def _confirmations():
    """How many blocks deep a block must be before we pay it out.

    Empirical basis (measured on this archive, whole Berkeley era, 211k events):
        depth 1 -> 210,827   depth 4 -> 7
        depth 2 ->     339   depth 5 -> 1
        depth 3 ->      26   depth 6 -> 1  <- deepest real reorg in 2 years
    (A 41-deep "reorg" also shows up in the data, but that is the Mesa hard
    fork cutting off the Berkeley tail, not a consensus reorg.)

    Mina's protocol finality is K=290, which is what chain_status='canonical'
    reflects. That is ~17h of waiting. CONFIRMATIONS_NUM trades that for a
    depth-based threshold; 20 gives a >3x margin over the worst reorg ever
    observed while cutting the wait to under an hour.
    """
    return int(_config().get("CONFIRMATIONS_NUM", 20))


def getChainTip():
    """Highest block height in the current era, regardless of chain_status."""
    h = _query_scalar(
        "SELECT MAX(height) FROM blocks WHERE protocol_version_id = %s",
        (_protocol_version_id(),),
    )
    return int(h or 0)


def _mature_height():
    """Highest height that has enough confirmations to be paid out."""
    return getChainTip() - _confirmations()


def _protocol_version_id():
    global _pv_cache
    if _pv_cache is not None:
        return _pv_cache
    cfg = _config().get("PROTOCOL_VERSION_ID")
    if cfg:
        _pv_cache = int(cfg)
    else:
        _pv_cache = int(_query_scalar("SELECT MAX(protocol_version_id) FROM blocks"))
    _log(f"  protocol era: protocol_version_id={_pv_cache}")
    return _pv_cache


# --- Postgres connection ---------------------------------------------------

_db_conn_cache = None


def _db():
    """Return a (cached) read-only psycopg2 connection to archive DB."""
    global _db_conn_cache
    if _db_conn_cache is None or _db_conn_cache.closed:
        c = _config()
        url = c.get("ARCHIVE_DB_URL")
        if not url:
            raise RuntimeError(
                "ARCHIVE_DB_URL is not set in config.yml. "
                "Example: postgresql://postgres:postgres@127.0.0.1:5432/archive"
            )
        _db_conn_cache = psycopg2.connect(url)
        _db_conn_cache.set_session(readonly=True, autocommit=True)
    return _db_conn_cache


def _query_dict(sql, params=None):
    with _db().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def _query_scalar(sql, params=None):
    with _db().cursor() as cur:
        cur.execute(sql, params or ())
        row = cur.fetchone()
        return row[0] if row else None


# --- daemon GraphQL (for current/next ledger hash) -------------------------

def _daemon_url():
    c = _config()
    return f"http://{c.get('GRAPHQL_HOST', '127.0.0.1')}:{c.get('GRAPHQL_PORT', 3085)}/graphql"


def _daemon_query(query, variables=None):
    payload = {"query": " ".join(query.split())}
    if variables:
        payload["variables"] = variables
    r = _session.post(_daemon_url(), json=payload, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "errors" in j:
        raise RuntimeError(f"daemon GraphQL errors: {j['errors']}")
    return j


# --- GCS helpers (staking ledger fetch) ------------------------------------

def _gcs_list(bucket, prefix, max_results=50):
    url = f"{GCS_LISTING}/{bucket}/o"
    params = {
        "prefix": prefix,
        "fields": "items(name,size,updated)",
        "maxResults": str(max_results),
    }
    r = _session.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json().get("items") or []


def _ensure_cache():
    _LEDGERS_CACHE.mkdir(parents=True, exist_ok=True)


def _resolve_ledger_filename(epoch, ledger_hash):
    items = _gcs_list(
        "mina-staking-ledgers",
        prefix=f"staking-{epoch}-{ledger_hash}-",
        max_results=50,
    )
    if not items:
        raise RuntimeError(f"no staking ledger found in GCS for epoch={epoch} hash={ledger_hash}")
    items.sort(key=lambda it: it["name"], reverse=True)
    return items[0]["name"]


def _download_ledger(epoch, ledger_hash):
    _ensure_cache()
    cached = _LEDGERS_CACHE / f"epoch-{epoch}-{ledger_hash}.json"
    if cached.exists():
        return json.loads(cached.read_bytes())
    fname = _resolve_ledger_filename(epoch, ledger_hash)
    url = f"{GCS_LEDGERS_URL}/{fname}"
    _log(f"  downloading staking ledger {fname} (~286 MB, cached after first run)...")
    t0 = time.time()
    with _session.get(url, timeout=600, stream=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("Content-Length") or 0) or None
        tmp = cached.with_suffix(cached.suffix + ".tmp")
        if _HAS_TQDM:
            bar = tqdm(total=total, unit="B", unit_scale=True, unit_divisor=1024,
                       desc="  ledger", dynamic_ncols=True)
        else:
            bar = None
            last_log = t0
            done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                if bar is not None:
                    bar.update(len(chunk))
                else:
                    done += len(chunk)
                    now = time.time()
                    if now - last_log >= 2:
                        rate = done / max(now - t0, 0.01) / 1024 / 1024
                        _log(f"    ledger: {done / 1024 / 1024:.1f} MB  @ {rate:.1f} MB/s")
                        last_log = now
        if bar is not None:
            bar.close()
        tmp.replace(cached)
    _log(f"  ledger downloaded in {time.time()-t0:.1f}s, {cached.stat().st_size:,} bytes")
    return json.loads(cached.read_bytes())


def _compute_untimed_slot(timing):
    """Slot at which a timed account becomes fully unlocked.

    Mirrors original Staking.py logic. All amount fields scale uniformly so the
    ratio is dimensionless — works whether stored as MINA strings or nanomina.
    """
    try:
        cliff_time = int(timing.get("cliff_time", 0))
        vest_period = int(timing.get("vesting_period", 0))
        cliff_amount = Decimal(str(timing.get("cliff_amount", 0)))
        initial = Decimal(str(timing.get("initial_minimum_balance", 0)))
        vest_incr = Decimal(str(timing.get("vesting_increment", 0)))
    except Exception:
        return 0
    if vest_period == 0 or vest_incr == 0:
        return cliff_time
    n_steps = int((initial - cliff_amount) // vest_incr)
    return cliff_time + n_steps * vest_period


# --- public API ------------------------------------------------------------

def getLatestHeight():
    """Chain tip of the current protocol era.

    Returns the raw tip - calc_rewards.py subtracts CONFIRMATIONS_NUM from it
    to get the payout cutoff, so the confirmation gate lives there.
    """
    return {"data": {"blocks": [{"blockHeight": getChainTip()}]}}


_consensus_cfg_cache = None


def getConsensusConfig():
    """Consensus parameters from the daemon, for slot <-> wall-clock maths.

    Returns {slot_duration_s, epoch_duration_s, slots_per_epoch, k, delta,
             genesis_ms} or None if the daemon is unreachable.

    Mesa changed slot duration from 180s to 90s, so anything time-related has
    to be read from the daemon rather than hardcoded.
    """
    global _consensus_cfg_cache
    if _consensus_cfg_cache is not None:
        return _consensus_cfg_cache
    try:
        j = _daemon_query("""
            { daemonStatus { consensusConfiguration {
                slotDuration epochDuration slotsPerEpoch k delta
                genesisStateTimestamp } } }
        """)
        cc = j["data"]["daemonStatus"]["consensusConfiguration"]
        ts = cc["genesisStateTimestamp"]          # "2026-09-03 18:00:00.000000Z"
        import datetime as _dt
        g = _dt.datetime.strptime(ts.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_dt.timezone.utc)
        _consensus_cfg_cache = {
            "slot_duration_s": int(cc["slotDuration"]) / 1000,
            "epoch_duration_s": int(cc["epochDuration"]) / 1000,
            "slots_per_epoch": int(cc["slotsPerEpoch"]),
            "k": int(cc["k"]),
            "delta": int(cc["delta"]),
            "genesis_ms": int(g.timestamp() * 1000),
        }
        return _consensus_cfg_cache
    except Exception as e:
        _log(f"  (could not read consensus config: {e})")
        return None


def slotToTimestamp(global_slot):
    """Wall-clock time (unix seconds) of a global-slot-since-hard-fork."""
    cfg = getConsensusConfig()
    if not cfg:
        return None
    return cfg["genesis_ms"] / 1000 + int(global_slot) * cfg["slot_duration_s"]


def getNextBlockProduction():
    """The validator's next scheduled block, per the daemon's VRF evaluation.

    Only a node that holds the block-producer key answers this; a plain
    follower returns null. Used to decide whether an epoch's payout is final
    or whether another coinbase is still on the way.

    Returns:
      {"scheduled": False}                      nothing won / not a producer
      {"scheduled": True, "epoch": int, "slot": int, "global_slot": int,
       "start_time_ms": int, "seconds_from_now": float}
    or None when the daemon cannot be reached.
    """
    try:
        j = _daemon_query("""
            { daemonStatus {
                nextBlockProduction {
                  times { epoch slot globalSlot startTime endTime }
                }
              } }
        """)
    except Exception as e:
        _log(f"  (could not query next block production: {e})")
        return None

    nbp = ((j.get("data") or {}).get("daemonStatus") or {}).get("nextBlockProduction")
    times = (nbp or {}).get("times") or []
    if not times:
        # Either no slot won for the evaluated window, or this node holds no
        # producer key (a follower always reports null here).
        return {"scheduled": False}

    t = times[0]
    start_ms = int(t["startTime"]) if t.get("startTime") is not None else None
    return {
        "scheduled": True,
        "epoch": int(t["epoch"]),
        "slot": int(t["slot"]),
        "global_slot": int(t["globalSlot"]),
        "start_time_ms": start_ms,
        "seconds_from_now": (start_ms / 1000.0 - time.time()) if start_ms else None,
    }


def getAccountBalance(public_key):
    """Live balance of an account, straight from the daemon.

    Returns a dict in MINA:
      {total, liquid, locked, nonce, inferred_nonce, pending_txs, block_height}
    or None when the daemon is unreachable.

    `inferred_nonce` counts transactions still sitting in the mempool, so
    `inferred_nonce - nonce` is the number of outbound txs not yet in a block.
    """
    try:
        j = _daemon_query("""
            { account(publicKey: "%s") {
                balance { total liquid locked blockHeight }
                nonce
                inferredNonce
              } }
        """ % public_key)
        a = (j.get("data") or {}).get("account")
        if not a:
            return None
        bal = a.get("balance") or {}

        def _mina(v):
            return (int(v) / NANOMINA) if v is not None else None

        nonce = int(a["nonce"]) if a.get("nonce") is not None else None
        inferred = int(a["inferredNonce"]) if a.get("inferredNonce") is not None else None
        return {
            "total": _mina(bal.get("total")),
            "liquid": _mina(bal.get("liquid")),
            "locked": _mina(bal.get("locked")),
            "block_height": int(bal["blockHeight"]) if bal.get("blockHeight") else None,
            "nonce": nonce,
            "inferred_nonce": inferred,
            "pending_txs": (inferred - nonce) if (nonce is not None and inferred is not None) else None,
        }
    except Exception as e:
        _log(f"  (could not fetch balance from daemon: {e})")
        return None


def _avg_seconds_per_block(sample=500):
    """Average seconds between blocks in the current era.

    Measured over the last `sample` heights. Slots are 90s but not every slot
    is won, so the real interval is usually 3-4 minutes — hence the empirical
    measurement instead of using the nominal slot time.
    """
    v = _query_scalar(
        """
        SELECT (MAX(t.ts) - MIN(t.ts)) / 1000.0 / NULLIF(COUNT(*) - 1, 0)
        FROM (
          SELECT height, MIN(timestamp::bigint) AS ts
          FROM blocks
          WHERE protocol_version_id = %s
          GROUP BY height
          ORDER BY height DESC
          LIMIT %s
        ) t
        """,
        (_protocol_version_id(), int(sample)),
    )
    return float(v) if v else None


def getBlockStatusReport(creator, epoch):
    """Every block produced by `creator` in the epoch, canonical or not.

    Returns:
      {
        "blocks": [ {height, state_hash, chain_status, coinbase_mina,
                     ts, blocks_to_canonical, eta_seconds}, ... ],
        "canonical_tip": int,
        "chain_tip": int,
        "k": int,
        "sec_per_block": float | None,
      }

    A block turns canonical at depth K=290. While it is still pending it must
    not be paid out (it can still be orphaned), but it should be visible.
    """
    epoch = int(epoch)
    pv_id = _protocol_version_id()
    spe = _slots_per_epoch()

    canonical_tip = _query_scalar(
        "SELECT MAX(height) FROM blocks "
        "WHERE chain_status = 'canonical' AND protocol_version_id = %s",
        (pv_id,),
    )
    chain_tip = _query_scalar(
        "SELECT MAX(height) FROM blocks WHERE protocol_version_id = %s",
        (pv_id,),
    )
    canonical_tip = int(canonical_tip or 0)
    chain_tip = int(chain_tip or 0)
    k = chain_tip - canonical_tip if chain_tip and canonical_tip else 290
    conf = _confirmations()
    mature_h = chain_tip - conf

    rows = _query_dict(
        """
        SELECT
          b.height,
          b.state_hash,
          b.chain_status::text AS chain_status,
          b.timestamp::bigint  AS ts,
          COALESCE((
            SELECT ic.fee::bigint
            FROM blocks_internal_commands bic
            JOIN internal_commands ic ON ic.id = bic.internal_command_id
            WHERE bic.block_id = b.id AND ic.command_type = 'coinbase'
            LIMIT 1
          ), 0) AS coinbase
        FROM blocks b
        JOIN public_keys pkc ON pkc.id = b.creator_id
        WHERE pkc.value = %(creator)s
          AND b.protocol_version_id = %(pv_id)s
          AND (b.global_slot_since_hard_fork / %(spe)s) = %(epoch)s
        ORDER BY b.height DESC
        """,
        {"creator": creator, "pv_id": pv_id, "spe": spe, "epoch": epoch},
    )

    spb = _avg_seconds_per_block()

    out = []
    for r in rows:
        h = int(r["height"])
        status = r["chain_status"]
        confs = max(0, chain_tip - h)
        to_go = max(0, h - mature_h)          # blocks left until payable
        out.append({
            "height": h,
            "state_hash": r["state_hash"],
            "chain_status": status,
            "ts": int(r["ts"]),
            "coinbase_mina": int(r["coinbase"]) / NANOMINA,
            "confirmations": confs,
            "mature": status != "orphaned" and confs >= conf,
            "blocks_to_mature": to_go,
            "eta_seconds": (to_go * spb) if (spb and to_go) else 0,
        })

    return {
        "blocks": out,
        "canonical_tip": canonical_tip,
        "chain_tip": chain_tip,
        "k": k,
        "confirmations_required": conf,
        "sec_per_block": spb,
    }


def ensureStakingLedger(epoch, ledger_hash):
    """Return the staking ledger for (epoch, hash), downloading it if needed.

    Ledgers live in gs://mina-staking-ledgers as
    staking-{epoch}-{hash}-{checksum}-{date}.json (~280 MB) and are cached
    under archive/cache/ledgers/. Public wrapper so other tools can reuse the
    same cache instead of each keeping their own copy.
    """
    return _download_ledger(int(epoch), ledger_hash)


def stakingLedgerPath(epoch, ledger_hash):
    """Local cache path for a staking ledger (may not exist yet)."""
    return _LEDGERS_CACHE / f"epoch-{int(epoch)}-{ledger_hash}.json"


def getBaseCoinbase():
    """Base coinbase (in MINA) for the current protocol era, read from archive.

    Mesa (MIP6) halved the coinbase 720 -> 360 because slots went from 180s to
    90s (same emission, twice as many blocks). Hardcoding it is a trap, so we
    take the most frequent coinbase value of the current era instead.

    Used only as the THRESHOLD for supercharged-block detection. (Supercharged
    rewards have long been inactive on mainnet — every block pays exactly the
    base coinbase — but keeping the threshold correct costs nothing.)
    """
    v = _query_scalar(
        """
        SELECT ic.fee
        FROM internal_commands ic
        JOIN blocks_internal_commands bic ON bic.internal_command_id = ic.id
        JOIN blocks b ON b.id = bic.block_id
        WHERE ic.command_type = 'coinbase'
          AND b.protocol_version_id = %s
        GROUP BY ic.fee
        ORDER BY count(*) DESC
        LIMIT 1
        """,
        (_protocol_version_id(),),
    )
    if v is None:
        _log("  WARNING: no coinbase found for current era, falling back to 720")
        return 720
    coinbase = int(v) / NANOMINA
    _log(f"  base coinbase for this era: {coinbase} MINA")
    return coinbase


def getEpochBlockRange(epoch):
    """Height range of an epoch within the current protocol era.

    Replaces the dead api.minastakes.com — computed straight from the archive.
    Orphaned blocks are excluded; depth-based maturity is applied later via
    CONFIRMATIONS_NUM in calc_rewards.py.
    Returns (min_height, max_height), or (None, None) if there are no blocks.
    """
    epoch = int(epoch)
    rows = _query_dict(
        """
        SELECT MIN(height) AS min_h, MAX(height) AS max_h, count(*) AS n
        FROM blocks
        WHERE chain_status <> 'orphaned'
          AND protocol_version_id = %(pv_id)s
          AND (global_slot_since_hard_fork / %(spe)s) = %(epoch)s
        """,
        {"pv_id": _protocol_version_id(), "spe": _slots_per_epoch(), "epoch": epoch},
    )
    r = rows[0] if rows else {}
    if not r.get("n"):
        return (None, None)
    return (int(r["min_h"]), int(r["max_h"]))


def getLedgerHash(epoch):
    """Resolve the staking-ledger hash used IN the given epoch.

    Strategy:
      1. Ask daemon for current/next epoch hashes (cheap)
      2. Otherwise scan GCS bucket for `staking-{epoch}-*.json`
    """
    epoch = int(epoch)
    try:
        j = _daemon_query("""
            { bestChain(maxLength: 1) {
                protocolState { consensusState {
                  epoch
                  stakingEpochData { ledger { hash } }
                  nextEpochData    { ledger { hash } }
                } } } }
        """)
        cs = j["data"]["bestChain"][0]["protocolState"]["consensusState"]
        cur = int(cs["epoch"])
        if epoch == cur:
            h = cs["stakingEpochData"]["ledger"]["hash"]
            _hash_to_epoch[h] = epoch
            return h
        if epoch == cur + 1:
            h = cs["nextEpochData"]["ledger"]["hash"]
            _hash_to_epoch[h] = epoch
            return h
    except Exception as e:
        _log(f"  (daemon GraphQL unreachable: {e}; falling back to GCS bucket)")

    items = _gcs_list(
        "mina-staking-ledgers",
        prefix=f"staking-{epoch}-",
        max_results=200,
    )
    if not items:
        raise RuntimeError(f"no staking ledger found in GCS for epoch {epoch}")
    items.sort(key=lambda it: it.get("updated", ""), reverse=True)
    m = re.match(rf"staking-{epoch}-([^-]+)-", items[0]["name"])
    if not m:
        raise RuntimeError(f"unrecognised ledger filename: {items[0]['name']}")
    h = m.group(1)
    _hash_to_epoch[h] = epoch
    return h


def _resolve_epoch_for_hash(ledger_hash):
    """Look up which epoch a ledger hash belongs to."""
    # 1. Cache filled by getLedgerHash() - fastest path
    if ledger_hash in _hash_to_epoch:
        return _hash_to_epoch[ledger_hash]

    # 2. Ask the daemon (knows current / next epoch)
    cur = None
    try:
        j = _daemon_query("""
            { bestChain(maxLength: 1) {
                protocolState { consensusState {
                  epoch
                  stakingEpochData { ledger { hash } }
                  nextEpochData    { ledger { hash } }
                } } } }
        """)
        cs = j["data"]["bestChain"][0]["protocolState"]["consensusState"]
        cur = int(cs["epoch"])
        if cs["stakingEpochData"]["ledger"]["hash"] == ledger_hash:
            _hash_to_epoch[ledger_hash] = cur
            return cur
        if cs["nextEpochData"]["ledger"]["hash"] == ledger_hash:
            _hash_to_epoch[ledger_hash] = cur + 1
            return cur + 1
    except Exception:
        pass

    # 3. Full GCS scan: from the current epoch (or 100 if the daemon is down)
    #    back to zero. Each step is a single cheap prefix lookup.
    top = (cur + 1) if cur is not None else 100
    for try_epoch in range(top, -1, -1):
        items = _gcs_list(
            "mina-staking-ledgers",
            prefix=f"staking-{try_epoch}-{ledger_hash}-",
            max_results=1,
        )
        if items:
            _hash_to_epoch[ledger_hash] = try_epoch
            return try_epoch
    raise RuntimeError(f"could not resolve epoch for ledger hash {ledger_hash}")


def getStakingLedger(variables):
    """Return delegators of `delegate` from the staking ledger at `ledgerHash`."""
    delegate = variables["delegate"]
    ledger_hash = variables["ledgerHash"]
    epoch = _resolve_epoch_for_hash(ledger_hash)
    ledger = _download_ledger(epoch, ledger_hash)

    stakes = []
    for acc in ledger:
        if acc.get("delegate") != delegate:
            continue
        timing = acc.get("timing")
        if timing:
            timing = dict(timing)
            timing["untimed_slot"] = _compute_untimed_slot(timing)
            timing["timed_epoch_end"] = False
        try:
            balance = float(acc.get("balance", 0))
        except (ValueError, TypeError):
            balance = 0.0
        stakes.append({
            "public_key": acc.get("pk"),
            "balance": balance,
            "chainId": None,
            "timing": timing,
        })
    return {"data": {"stakes": stakes}}


# --- the big one: getBlocks via SQL ----------------------------------------

# Note: the schema is the Berkeley v3 archive layout. Column names that may
# differ across versions:
#   internal_commands.command_type   (some installs: type)
#   user_commands.command_type       (some installs: type)
#   blocks_internal_commands.status  (sometimes absent)
#   blocks_user_commands.status      (sometimes failure_reason instead)
# We probe at startup once and adapt.

_schema_probe = None


def _probe_schema():
    """Discover actual column names in this Postgres for fee/command tables."""
    global _schema_probe
    if _schema_probe is not None:
        return _schema_probe

    def has_col(table, col):
        row = _query_scalar(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            (table, col),
        )
        return row is not None

    ic_type_col = "command_type" if has_col("internal_commands", "command_type") else "type"
    uc_type_col = "command_type" if has_col("user_commands", "command_type") else "type"
    bic_has_status = has_col("blocks_internal_commands", "status")
    buc_has_status = has_col("blocks_user_commands", "status")

    _schema_probe = {
        "ic_type_col": ic_type_col,
        "uc_type_col": uc_type_col,
        "bic_has_status": bic_has_status,
        "buc_has_status": buc_has_status,
    }
    _log(f"  schema probe: {_schema_probe}")
    return _schema_probe


def getBlocks(variables):
    """
    Return canonical blocks created by `creator` in the requested epoch and
    height range, in the MinaExplorer-style shape calc_rewards.py expects.
    """
    creator = variables["creator"]
    epoch = int(variables["epoch"])
    h_min = int(variables["blockHeightMin"])
    h_max = int(variables["blockHeightMax"])
    spe = _slots_per_epoch()
    pv_id = _protocol_version_id()

    s = _probe_schema()
    ic_type = s["ic_type_col"]
    uc_type = s["uc_type_col"]
    bic_status_clause = "AND bic.status = 'applied'" if s["bic_has_status"] else ""
    buc_status_clause = "AND buc.status = 'applied'" if s["buc_has_status"] else ""

    sql = f"""
WITH our_blocks AS (
  SELECT
    b.id AS bid,
    b.height,
    b.state_hash,
    b.global_slot_since_genesis AS slot_g,
    b.global_slot_since_hard_fork AS slot_hf,
    pkw.value AS winner
  FROM blocks b
  JOIN public_keys pkc ON pkc.id = b.creator_id
  JOIN public_keys pkw ON pkw.id = b.block_winner_id
  WHERE pkc.value = %(creator)s
    -- Not 'canonical' but "not known-orphaned": chain_status only flips to
    -- canonical at K=290, while the payout gate is the depth-based
    -- CONFIRMATIONS_NUM already applied to h_max by the caller.
    AND b.chain_status <> 'orphaned'
    AND b.height BETWEEN %(h_min)s AND %(h_max)s
    AND b.protocol_version_id = %(pv_id)s
    AND (b.global_slot_since_hard_fork / %(spe)s) = %(epoch)s
),
block_coinbase AS (
  SELECT
    bic.block_id,
    ic.fee::bigint AS coinbase_fee,
    pk.value      AS coinbase_receiver
  FROM blocks_internal_commands bic
  JOIN internal_commands ic ON ic.id = bic.internal_command_id
  JOIN public_keys pk       ON pk.id = ic.receiver_id
  WHERE ic.{ic_type} = 'coinbase'
    {bic_status_clause}
    AND bic.block_id IN (SELECT bid FROM our_blocks)
),
block_snark AS (
  SELECT
    bic.block_id,
    SUM(ic.fee::bigint) AS snark_total
  FROM blocks_internal_commands bic
  JOIN internal_commands ic ON ic.id = bic.internal_command_id
  WHERE ic.{ic_type} IN ('fee_transfer', 'fee_transfer_via_coinbase')
    {bic_status_clause}
    AND bic.block_id IN (SELECT bid FROM our_blocks)
  GROUP BY bic.block_id
),
block_txfee AS (
  SELECT
    buc.block_id,
    SUM(uc.fee::bigint) AS tx_total
  FROM blocks_user_commands buc
  JOIN user_commands uc ON uc.id = buc.user_command_id
  WHERE 1=1
    {buc_status_clause}
    AND buc.block_id IN (SELECT bid FROM our_blocks)
  GROUP BY buc.block_id
)
SELECT
  ob.height,
  ob.state_hash,
  ob.slot_g,
  ob.slot_hf,
  ob.winner,
  COALESCE(bc.coinbase_fee, 0)   AS coinbase,
  bc.coinbase_receiver,
  COALESCE(bs.snark_total, 0)    AS snark_fees,
  COALESCE(bt.tx_total, 0)       AS tx_fees
FROM our_blocks ob
LEFT JOIN block_coinbase bc ON bc.block_id = ob.bid
LEFT JOIN block_snark    bs ON bs.block_id = ob.bid
LEFT JOIN block_txfee    bt ON bt.block_id = ob.bid
ORDER BY ob.height DESC;
"""

    _log(f"  querying SQL for blocks: creator={creator[:10]}..., epoch={epoch}, "
         f"heights={h_min}..{h_max}")
    t0 = time.time()
    rows = _query_dict(sql, {
        "creator": creator,
        "h_min": h_min,
        "h_max": h_max,
        "spe": spe,
        "epoch": epoch,
        "pv_id": pv_id,
    })
    _log(f"  SQL returned {len(rows)} rows in {time.time()-t0:.2f}s")

    blocks = []
    for r in rows:
        has_coinbase = r["coinbase"] and int(r["coinbase"]) > 0
        blocks.append({
            "blockHeight": int(r["height"]),
            "canonical": True,
            "creator": creator,
            "txFees": str(int(r["tx_fees"])),
            "snarkFees": str(int(r["snark_fees"])),
            "stateHash": r["state_hash"],
            "winnerAccount": {"publicKey": r["winner"]},
            "protocolState": {
                "consensusState": {
                    "blockHeight": int(r["height"]),
                    "epoch": epoch,
                    "slotSinceGenesis": int(r["slot_g"]),
                },
            },
            "transactions": {
                "coinbase": str(int(r["coinbase"])) if has_coinbase else "0",
                "coinbaseReceiverAccount": (
                    {"publicKey": r["coinbase_receiver"]} if has_coinbase else None
                ),
                "feeTransfer": [],  # not consumed by calc_rewards.py
            },
        })
    return {"data": {"blocks": blocks}}
