# mina-payout-script

**English** · [Русский](README.ru.md)

Reward calculation and payout tooling for a [Mina Protocol](https://minaprotocol.com)
block producer, working against **your own archive node** instead of third-party
explorer APIs.

Updated for the **Mesa hard fork** (3 September 2026): 90-second slots, 360 MINA
coinbase, reset epoch numbering.

---

## Contents

- [Why self-hosted data](#why-self-hosted-data)
- [What's in the box](#whats-in-the-box)
- [Requirements](#requirements)
- [Install](#install)
- [Configuration](#configuration)
- [Connecting to your node](#connecting-to-your-node)
- [Daily use](#daily-use)
  - [1. Calculate rewards](#1-calculate-rewards)
  - [2. Read the report](#2-read-the-report)
  - [3. Send payouts](#3-send-payouts)
  - [4. Withdraw commission](#4-withdraw-commission)
- [Payout redirects](#payout-redirects)
- [VRF slot prediction](#vrf-slot-prediction)
- [Running an archive node](#running-an-archive-node)
- [How the calculation works](#how-the-calculation-works)
- [Confirmations and reorg safety](#confirmations-and-reorg-safety)
- [Mesa hard fork notes](#mesa-hard-fork-notes)
- [Shell aliases](#shell-aliases)
- [Troubleshooting](#troubleshooting)
- [Security notes](#security-notes)

---

## Why self-hosted data

The Mina ecosystem's public data APIs have not aged well. `graphql.minaexplorer.com`
stopped resolving, `api.minastakes.com` returns 500s, and the Foundation's S3
bucket of precomputed blocks was deleted without notice. Anything built on those
is dead.

This project reads from **your own archive node's Postgres**, plus the daemon's
GraphQL for live state. The only external dependency is the public GCS bucket of
staking ledgers, which is still maintained — and the block guardian falls back
across several mirrors when one disappears.

## What's in the box

| Path | What it does |
| --- | --- |
| `calc_rewards.py` | Works out each delegator's share, writes `e<N>_payouts.csv` |
| `send_payout.py` | Sends the payouts from that CSV |
| `withdraw.py` | Moves the validator's commission to a cold wallet |
| `GraphQL.py` | Data layer: archive SQL, daemon GraphQL, staking ledger cache |
| `mina_client.py` | Small GraphQL client for wallet and payment operations |
| `vrf/probe.py` | Predicts which slots you win, a whole epoch ahead |
| `archive/` | Docker Compose stack for an archive node, with a fixed block guardian |
| `scripts/` | Installer, SSH tunnel, shell aliases |
| `calc.sh` | Convenience wrapper around `calc_rewards.py` |

## Requirements

- Python 3.9+
- A Mina **archive node** with its Postgres reachable (see [Running an archive node](#running-an-archive-node))
- A Mina **daemon** holding the payout wallet key, GraphQL reachable
- `jq` (used by `calc.sh`)
- ~300 MB of disk per cached staking ledger

The daemon and archive can live on a remote server; everything talks to
`127.0.0.1` through an SSH tunnel.

## Install

```bash
git clone https://github.com/etcusr/mina-payout-script.git
cd mina-payout-script
./scripts/install.sh
```

The installer creates a virtualenv, installs dependencies, and copies
`config.example.yml` to `config.yml`. It never overwrites an existing config.

## Configuration

Everything lives in `config.yml`, which is gitignored. Start from
`config.example.yml` — every key is documented there. The essentials:

```yaml
VALIDATOR_ADDRESS: B62q...        # your block producer
SEND_FROM_ADDRESS: B62q...        # wallet receiving the coinbase, sends payouts
WITHDRAW_TO_ADDRESS: B62q...      # cold wallet for commission
VALIDATOR_FEE: 0.05               # 5%
GRAPHQL_HOST: 127.0.0.1
GRAPHQL_PORT: 3085
ARCHIVE_DB_URL: "postgresql://postgres:postgres@127.0.0.1:5432/archive"
```

**The wallet password is never stored.** `send_payout.py` and `withdraw.py`
prompt for it at runtime with hidden input, so it stays out of shell history and
off disk.

## Connecting to your node

If the node is remote, open a tunnel first — it forwards both the daemon
GraphQL and the archive Postgres:

```bash
./scripts/tunnel.sh my-node --bg     # ssh alias, user@host, whatever ssh takes
./scripts/tunnel.sh --stop
```

The GraphQL endpoint must point at the daemon that **holds the wallet key**,
since that is where payments get signed. If your daemon exposes GraphQL on a
different remote port (a Docker mapping, for instance):

```bash
REMOTE_GRAPHQL_PORT=6521 ./scripts/tunnel.sh my-node --bg
```

## Daily use

### 1. Calculate rewards

```bash
./calc.sh                  # current epoch, taken from the daemon
./calc.sh 3                # epoch 3
./calc.sh 3 --vrf          # plus the VRF scan (see below)
```

This writes `e<N>_payouts.csv` — one line per destination address:

```
B62qDelegator...;3348000000000;3348.0;common;unlocked
address;nanomina;mina;delegation_type;lock_status
```

### 2. Read the report

```
==========================================================================
  EPOCH 0   chain tip 549218  |  canonical tip 548928  |  payout threshold 20 confs
==========================================================================

    height  time (UTC)          coinbase  archive    confs  note
--  ------  ----------------  ----------  --------  ------  ----------------
+   549174  2026-09-06 06:46         360  pending       44  ready to pay out
+   548900  2026-09-05 15:09         360  canonical    318  ready to pay out
~   549300  2026-09-06 09:12         360  pending        3  17 more blocks (~1h)
x   548501  2026-09-04 12:01         360  orphaned       -  orphaned, no reward

  'archive' is Postgres' own chain_status, which only flips to canonical at K=290.
  Payout eligibility is the 'confs' column against CONFIRMATIONS_NUM=20.
```

| Marker | Meaning |
| --- | --- |
| `+` green | Enough confirmations, included in this payout |
| `~` yellow | Produced but still too shallow, excluded for now |
| `x` red | Orphaned, no reward |

The verdict at the bottom tells you whether to act:

| Verdict | Meaning |
| --- | --- |
| `READY` | Epoch is settled, no more blocks expected — safe to pay out |
| `PARTIAL` | Payable now, but more coinbase is still coming this epoch |
| `WAIT` | Blocks found, none confirmed deeply enough yet |

`READY` can appear **before the epoch ends** — if the VRF scan shows no
remaining won slots, there is nothing left to wait for.

### 3. Send payouts

```bash
python3 send_payout.py --epoch 3
```

Reads `e3_payouts.csv`, unlocks the wallet, sends one transaction per line, then
watches until each is included. Anything unconfirmed within
`TX_CHECK_TIMER_SECONDS` is written to `failed_payouts_3.csv` so you can retry.

### 4. Withdraw commission

```bash
python3 withdraw.py --amount 252.5
python3 withdraw.py --amount 252.5 --to B62qOther...   # override destination
```

The amount is deliberately manual: the wallet balance does not account for
payouts still sitting in the mempool, so an automatic "everything minus a
buffer" can easily send money you have already promised to delegators.

## Payout redirects

A reward does not have to go to the address that holds the stake. Two things
this solves:

- you stake from several of your own addresses and want everything landing on
  one, instead of collecting from all of them afterwards
- an account is shared with someone and the reward has to be split

```yaml
PAYOUT_REDIRECTS:
  # everything to a single address
  B62qMyStakingOne...: B62qMyMainWallet...
  B62qMyStakingTwo...: B62qMyMainWallet...

  # split between two people
  B62qSharedAccount...:
    B62qPartner...:      50
    B62qMyMainWallet...: 50
```

The **share of the pool is still calculated from the staking address** — only
the destination of the transfer changes. Several staking addresses may resolve
to the same destination, in which case their payouts are merged into a single
transaction.

Shares are normalised, so `50/50`, `0.5/0.5` and `1/1` all mean the same thing.
The last destination absorbs the rounding remainder, so the parts always add
back up to the original amount exactly.

`MINIMUM_PAYOUT` is applied to the **final merged transfer**, not per delegator
— a share too small to pay on its own still counts once combined with others
going to the same place.

`calc_rewards.py` prints what it redirected:

```
Payout redirects active: 3
  B62qMyStakingOne...
    -> B62qMyMainWallet...  100%
  B62qSharedAccount...
    -> B62qPartner...  50%
    -> B62qMyMainWallet...  50%

Redirected payouts
  B62qMyMainWallet...  <- 128.44 MINA from 3 address(es)
      B62qMyStakingOne...
      B62qMyStakingTwo...
      B62qSharedAccount... (50%)
```

## VRF slot prediction

Mina evaluates a VRF per (delegator, slot) pair at the start of each epoch, so
the winning slots for the **whole epoch** are already determined. `vrf/probe.py`
asks the daemon to replay that evaluation:

```bash
python3 vrf/probe.py --epoch 0              # whole epoch
python3 vrf/probe.py --epoch 0 --from-now   # only slots still ahead
python3 vrf/probe.py 619                    # one specific slot
```

Output:

```
=== 10 winning slot(s) in epoch 0, slots 0..7139 ===
  slot    619 (epoch slot  619,  past)  idx     81   102,106.96 MINA  B62q...
  slot   1778 (epoch slot 1778, ahead)  idx     60    69,705.75 MINA  B62q...
  ...
```

Results are cached in `vrf/won_slots_epoch<N>.json` and reused by
`calc_rewards.py --vrf`, which turns the vague "another block is scheduled" into
an exact "the epoch fully settles on 10 Sep at 03:05 UTC".

**Cost.** Every (slot, delegator) pair is one elliptic-curve operation on the
producer node. A full epoch with 58 delegators is ~414,000 evaluations. Mina's
daemon is single-threaded (OCaml Async), so throughput tops out near 650
evaluations per second — about 10 minutes per epoch, whatever `--workers` says.
Extra workers only hide network latency.

**Requirements.** The block producer key must be unlockable in the daemon's
wallet. The script unlocks it, scans, and locks it back — on success, on error,
on Ctrl+C, and on SIGTERM. A follower node cannot do this: it holds no producer
key and returns nothing.

## Running an archive node

`archive/` contains a Docker Compose stack: Postgres, `mina-archive`, a
non-producing daemon that feeds it, and a **replacement block guardian**.

```bash
cd archive
docker compose up -d
```

The Foundation's own `missing-blocks-guardian` script hardcodes a single S3
bucket. When that bucket was deleted, the script retried the same missing block
forever, filling logs at hundreds of lines per second and never making progress.
`archive/scripts/blocks-guardian.sh` replaces it:

- tries several precomputed-block mirrors in order (GCS first, S3 second)
- validates the JSON before handing it to `mina-archive-blocks`
- blacklists blocks that are unavailable everywhere, instead of looping
- asks the daemon which blocks are actually missing, rather than guessing

Add more mirrors by appending to the `SOURCES` array in that script.

## How the calculation works

1. **Staking ledger.** Fetched for the epoch's ledger hash from
   `gs://mina-staking-ledgers` and cached under `archive/cache/ledgers/`
   (~280 MB each, downloaded once).
2. **Blocks.** Queried from the archive Postgres: blocks your validator created
   in that epoch, excluding orphans, at or beyond `CONFIRMATIONS_NUM` depth.
3. **Reward pool.** `coinbase + transaction fees - snark fees`, summed over
   those blocks. The coinbase is read from the database, never hardcoded — Mesa
   halved it from 720 to 360.
4. **Split.** Each delegator gets `pool * (their_stake / total_stake) * (1 - fee)`.
   Foundation and O(1) Labs stake can carry different fees
   (`VALIDATOR_FEE_FOUNDATION`, `VALIDATOR_FEE_O1LABS`); those address lists live
   in `foundation_addresses.txt` and `O(1)Labs.txt`.
5. **Filter.** Anything below `MINIMUM_PAYOUT` is dropped — not worth the fee.
6. **Redirect.** Destinations are rewritten per `PAYOUT_REDIRECTS` and merged.

Whatever is left in the wallet after paying everyone is the validator's
commission.

## Confirmations and reorg safety

A block only becomes spendable-safe once it is deep enough that a chain reorg
cannot remove it. Mina's protocol finality is **K=290** — that is what the
archive's `chain_status='canonical'` means, and it costs roughly 17 hours of
waiting.

Measured on a full archive across the entire Berkeley era, 211,202 reorg events:

| Reorg depth | Occurrences |
| --- | --- |
| 1 | 210,827 (99.82%) |
| 2 | 339 |
| 3 | 26 |
| 4 | 7 |
| 5 | 1 |
| 6 | 1 |

The deepest genuine reorg in two years was **6 blocks**. (A 41-deep outlier also
shows up in that data, but it is the Mesa hard fork truncating the Berkeley
tail, not a consensus reorg.)

`CONFIRMATIONS_NUM: 20` therefore carries more than a 3x margin over the worst
case ever observed, while cutting the wait from ~17 hours to under an hour. Set
it to 290 if you want strict protocol finality; the Mina Foundation's own
guidance for exchanges is 15.

## Mesa hard fork notes

Mesa activated on 3 September 2026 at 18:00 UTC and changed several things this
script depends on:

| | Berkeley | Mesa |
| --- | --- | --- |
| Slot duration | 180s | **90s** |
| Coinbase per block | 720 MINA | **360 MINA** |
| Slots per epoch | 7140 | 7140 (unchanged) |
| Epoch length | ~14.9 days | **~7.44 days** |
| `global_slot_since_hard_fork` | continuous | **reset to 0** |
| Epoch numbering | continuous | **reset to 0** |

The slot reset makes `slot_hf / 7140` ambiguous: Berkeley epoch 5 and Mesa
epoch 5 produce the same value. Eras are told apart by `protocol_version_id` in
the `blocks` table (1 = pre-Berkeley, 2 = Berkeley, 3 = Mesa), detected
automatically. To recalculate a historical epoch from an older era, set
`PROTOCOL_VERSION_ID` in `config.yml`.

`global_slot_since_genesis` did **not** reset, which is why delegator vesting
checks still work across the fork.

Archive schema changes were limited to zkApp tables, so the queries here were
unaffected.

## Shell aliases

```bash
echo "source /path/to/mina-payout-script/scripts/aliases.sh" >> ~/.zshrc
export MINA_SSH_HOST=my-node       # lets mina_tunnel run bare
```

| Alias | Does |
| --- | --- |
| `mina_calc [epoch] [flags]` | Calculate rewards |
| `mina_pay --epoch N` | Send payouts |
| `mina_withdraw --amount N` | Withdraw commission |
| `mina_vrf --epoch N` | Predict won slots |
| `mina_tunnel <host> --bg` | Open the SSH tunnel |
| `mina_tunnel --stop` | Close it |
| `mina_cd` | cd into the project with the venv active |

## Troubleshooting

**`ModuleNotFoundError` right after moving the project**
A virtualenv stores absolute paths, so it breaks when the directory moves.
Recreate it: `rm -rf venv && ./scripts/install.sh`

**`No canonical blocks found for epoch N in the current protocol era`**
Either the epoch genuinely has no blocks, or you are asking for an epoch from an
older protocol era. Set `PROTOCOL_VERSION_ID` for the era you mean.

**`could not connect to server` on the Postgres URL**
The tunnel is down. `./scripts/tunnel.sh <host> --bg`

**`Couldn't find an unlocked key for specified sender` from `vrf/probe.py`**
`evaluateVrf` needs the producer key unlocked in the daemon's wallet. Check it
is there at all: `{ trackedAccounts { publicKey locked } }`. A follower node
never has it.

**VRF scan says `nextBlockProduction: null`**
You are talking to a follower, not the producer. Point `GRAPHQL_PORT` at the
node running the block producer key.

**Guardian logs the same block forever**
The old Foundation script. Switch to `archive/scripts/blocks-guardian.sh` as
wired up in `archive/docker-compose.yml`.

## Security notes

- `config.yml` holds your addresses and DB credentials — gitignored, keep it so.
- Wallet passwords are prompted for, never stored or logged.
- `send_payout.py` and `withdraw.py` lock the wallet again when they finish.
- `vrf/probe.py` unlocks the **block producer** key. It locks it back through
  `finally`, `atexit`, and signal handlers, and prints a manual lock command if
  all retries fail. Do not expose the daemon's GraphQL port publicly.
- Payout CSVs contain delegator addresses and amounts — gitignored as `*.csv`.

## License

See [LICENSE](LICENSE).
