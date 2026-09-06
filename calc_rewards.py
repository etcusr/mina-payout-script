from tabulate import tabulate
import GraphQL
import redirects
import os
import sys
import requests
import decimal
import datetime
import time
import yaml
from pprint import pprint
import argparse


class C:
    """ANSI colours, disabled automatically when not writing to a terminal."""
    _on = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    RESET  = "\033[0m"  if _on else ""
    BOLD   = "\033[1m"  if _on else ""
    DIM    = "\033[2m"  if _on else ""
    RED    = "\033[31m" if _on else ""
    GREEN  = "\033[32m" if _on else ""
    YELLOW = "\033[33m" if _on else ""
    BLUE   = "\033[34m" if _on else ""
    CYAN   = "\033[36m" if _on else ""
    GREY   = "\033[90m" if _on else ""

parser = argparse.ArgumentParser(description='Mina payouts script')
c = yaml.load(open('config.yml', encoding='utf8'), Loader=yaml.SafeLoader)
################################################################
# Define the payout calculation here
################################################################
public_key     = str(c["VALIDATOR_ADDRESS"])
parser.add_argument('--epoch', required=True, type=int, help='epoch number')
parser.add_argument('--vrf', action='store_true',
                    help='use the VRF scan to list every won slot of the epoch. '
                         'Reuses vrf/won_slots_epoch<N>.json when present; '
                         'otherwise runs the scan (~10 min, asks for the BP key '
                         'password). Without this flag only the daemon\'s next '
                         'scheduled slot is known.')
parser.add_argument('--vrf-refresh', action='store_true',
                    help='force a fresh VRF scan even if a cached one exists')
args = parser.parse_args()
staking_epoch  = int(args.epoch)
fee            = float(c["VALIDATOR_FEE"])
SP_FEE         = float(c["VALIDATOR_FEE_SP"])
foundation_fee = float(c["VALIDATOR_FEE_FOUNDATION"])
labs_fee       = float(c["VALIDATOR_FEE_O1LABS"])
min_height     = int(c["FIRST_BLOCK_HEIGHT"])  # This can be the last known payout or this could vary the query to be a starting date
latest_block   = int(c["LATEST_BLOCK_HEIGHT"])
confirmations  = int(c["CONFIRMATIONS_NUM"])  # Can set this to any value for min confirmations up to `k`
MINIMUM_PAYOUT = float(c["MINIMUM_PAYOUT"])
# Wallet the coinbase lands on and payouts are sent from (--coinbase-receiver)
payout_wallet  = str(c["SEND_FROM_ADDRESS"])
decimal_       = 1e9
# Base coinbase is read from the archive rather than hardcoded: Mesa (MIP6)
# halved it 720 -> 360 because slots went from 180s to 90s. It is only used as
# the supercharged-block threshold; the actual reward always comes from the DB.
COINBASE       = GraphQL.getBaseCoinbase()

print(f'EPOCH: {staking_epoch}')

if min_height == 0:
    # This used to call api.minastakes.com, which died after Berkeley.
    # The epoch range now comes straight from the archive DB, protocol era
    # aware (see _protocol_version_id in GraphQL.py).
    epoch_min, epoch_max = GraphQL.getEpochBlockRange(staking_epoch)
    if epoch_min is None:
        exit(f'No canonical blocks found for epoch {staking_epoch} '
             f'in the current protocol era')
    min_height = epoch_min
    print(f'Epoch {staking_epoch} canonical range from archive: '
          f'{epoch_min}..{epoch_max}')


with open("version", "r") as v_file:
    version = v_file.read()
print(f'Script version: {version}')


def float_to_string(number, precision=9):
    return '{0:.{prec}f}'.format(
        decimal.Context(prec=100).create_decimal(str(number)),
        prec=precision,
    ).rstrip('0').rstrip('.') or '0'


def write_to_file(data_string: str, file_name: str, mode: str = "w"):
    with open(file_name, mode) as some_file:
        some_file.write(data_string + "\n")


with open("foundation_addresses.txt", "r") as f:
    foundation_delegations = f.read().split("\n")

with open("O(1)Labs.txt", "r") as f:
    labs_delegations = f.read().split("\n")

try:
    ledger_hash = GraphQL.getLedgerHash(epoch=staking_epoch)
    print(ledger_hash)
    # ledger_hash = "jxsAidvKvEQJMC7Z2wkLrFGzCqUxpFMRhAj4K5o49eiFLhKSyXL"
    # ledger_hash = ledger_hash["data"]["blocks"][0] \
    #                          ["protocolState"]["consensusState"] \
    #                          ["stakingEpochData"]["ledger"]["hash"]
except Exception as e:
    print(e)
    exit("Issue getting ledger_hash from GraphQL")

if latest_block == 0:
    # Get the latest block height
    latest_block = GraphQL.getLatestHeight()
else:
    latest_block = {'data': {'blocks': [{'blockHeight': latest_block}]}}

if not latest_block:
    exit("Issue getting the latest height")
assert latest_block["data"]["blocks"][0]["blockHeight"] > 1

# Only ever pay out confirmed blocks
max_height = latest_block["data"]["blocks"][0]["blockHeight"] - confirmations
assert max_height <= latest_block["data"]["blocks"][0]["blockHeight"]

print(f"This script will payout from blocks {min_height} to {max_height}")


def _fmt_eta(seconds):
    seconds = int(seconds)
    if seconds <= 0:
        return "-"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"~{h}h {m}m"
    return f"~{m}m"


# --- Report on every block of the epoch, including not-yet-canonical ones ---
try:
    report = GraphQL.getBlockStatusReport(public_key, staking_epoch)
except Exception as report_err:
    print(f"(could not build block report: {report_err})")
    report = None

def _hr(char="-"):
    return C.GREY + char * 74 + C.RESET


if report:
    b_list = report["blocks"]
    req = report["confirmations_required"]

    print()
    print(C.BOLD + C.CYAN + "=" * 74 + C.RESET)
    print(f"{C.BOLD}{C.CYAN}  EPOCH {staking_epoch}{C.RESET}"
          f"{C.GREY}   chain tip {report['chain_tip']}"
          f"  |  canonical tip {report['canonical_tip']}"
          f"  |  payout threshold {req} confs{C.RESET}")
    print(C.BOLD + C.CYAN + "=" * 74 + C.RESET)

    mature, immature, orphans = [], [], []

    if not b_list:
        print(f"  {C.GREY}No blocks produced in this epoch.{C.RESET}")
    else:
        rows = []
        for b in b_list:
            ts = datetime.datetime.utcfromtimestamp(b["ts"] / 1000).strftime("%Y-%m-%d %H:%M")
            if b["chain_status"] == "orphaned":
                orphans.append(b)
                mark, col = "x", C.RED
                note = "orphaned, no reward"
            elif b["mature"]:
                mature.append(b)
                mark, col = "+", C.GREEN
                note = "ready to pay out"
            else:
                immature.append(b)
                mark, col = "~", C.YELLOW
                note = (f"{b['blocks_to_mature']} more blocks "
                        f"({_fmt_eta(b['eta_seconds'])})")
            rows.append([
                f"{col}{mark}{C.RESET}",
                f"{col}{b['height']}{C.RESET}",
                f"{C.GREY}{ts}{C.RESET}",
                f"{b['coinbase_mina']:g}",
                f"{C.GREY}{b['chain_status']}{C.RESET}",
                b["confirmations"],
                f"{col}{note}{C.RESET}",
            ])

        print()
        print(tabulate(rows,
                       headers=["", "height", "time (UTC)", "coinbase",
                                "archive", "confs", "note"],
                       tablefmt="simple"))
        print()
        print(f"  {C.GREY}'archive' is Postgres' own chain_status, which only "
              f"flips to canonical at K={report['k']}.{C.RESET}")
        print(f"  {C.GREY}Payout eligibility is the 'confs' column against "
              f"CONFIRMATIONS_NUM={req}.{C.RESET}")

        if report["sec_per_block"]:
            print(f"  {C.GREY}network pace ~{report['sec_per_block']/60:.1f} min/block "
                  f"(90s slots, not every slot is won){C.RESET}")

        if mature:
            gross = sum(b["coinbase_mina"] for b in mature)
            print(f"  {C.GREEN}{C.BOLD}[+]{C.RESET} {C.GREEN}{len(mature)} block(s), "
                  f"{gross:g} MINA gross{C.RESET} - included in the payout below")
        if immature:
            worst = max(immature, key=lambda x: x["eta_seconds"])
            gross = sum(b["coinbase_mina"] for b in immature)
            print(f"  {C.YELLOW}{C.BOLD}[~]{C.RESET} {C.YELLOW}{len(immature)} block(s), "
                  f"{gross:g} MINA below {req} confs{C.RESET} - last matures in "
                  f"{C.BOLD}{_fmt_eta(worst['eta_seconds'])}{C.RESET}")
        if orphans:
            gross = sum(b["coinbase_mina"] for b in orphans)
            print(f"  {C.RED}{C.BOLD}[x]{C.RESET} {C.RED}{len(orphans)} block(s) orphaned, "
                  f"{gross:g} MINA lost{C.RESET}")

    # --- Is another block still coming this epoch? --------------------------
    print(_hr())
    payout_now = True
    vrf = None

    # The VRF scan (if we have one) knows every won slot of the epoch, which is
    # far better than the daemon's "next scheduled slot" - it tells us exactly
    # when the epoch is done producing.
    if args.vrf or args.vrf_refresh:
        try:
            sys.path.insert(0, "vrf")
            import probe as vrf_probe
            if not args.vrf_refresh:
                vrf = vrf_probe.load_cached(staking_epoch, ledger_hash=ledger_hash)
            if vrf is None:
                why = "refresh requested" if args.vrf_refresh else "no cached scan"
                print(f"  {C.GREY}VRF scan: {why}, running it now "
                      f"(~10 min, needs the BP key password){C.RESET}\n")
                vrf = vrf_probe.scan_epoch(staking_epoch)
            else:
                print(f"  {C.GREY}VRF scan: reusing "
                      f"{vrf_probe.cached_path(staking_epoch)}{C.RESET}")
        except Exception as e:
            print(f"  {C.RED}VRF scan unavailable: {e}{C.RESET}")
            vrf = None

    if vrf:
        now = time.time()
        won = vrf["slots"]
        ahead = [s for s in won
                 if (GraphQL.slotToTimestamp(s["global_slot"]) or 0) > now]
        produced = len(won) - len(ahead)
        gross_all = len(won) * COINBASE
        print(f"  {C.BOLD}VRF: {len(won)} won slot(s) this epoch{C.RESET} "
              f"{C.GREY}({produced} passed, {len(ahead)} ahead) "
              f"= {gross_all:g} MINA gross total{C.RESET}")
        if ahead:
            payout_now = False
            for s in ahead:
                ts = GraphQL.slotToTimestamp(s["global_slot"])
                when = datetime.datetime.utcfromtimestamp(ts).strftime("%d %b %H:%M UTC")
                print(f"      {C.YELLOW}slot {s['global_slot']:>5}{C.RESET}  {when}"
                      f"  {C.GREY}in {_fmt_eta(ts - now)}{C.RESET}")
            last_ts = GraphQL.slotToTimestamp(ahead[-1]["global_slot"])
            settle = last_ts + req * (report["sec_per_block"] or 210)
            print(f"  {C.YELLOW}{C.BOLD}[~]{C.RESET} {C.YELLOW}last block of the epoch "
                  f"at slot {ahead[-1]['global_slot']}{C.RESET}, "
                  f"epoch fully settles "
                  f"{C.BOLD}{datetime.datetime.utcfromtimestamp(settle).strftime('%d %b %H:%M UTC')}"
                  f"{C.RESET} {C.GREY}({_fmt_eta(settle - now)} from now){C.RESET}")
        else:
            print(f"  {C.GREEN}{C.BOLD}[+]{C.RESET} {C.GREEN}All won slots of this epoch "
                  f"have passed{C.RESET} - nothing more is coming")
    else:
        nbp = GraphQL.getNextBlockProduction()
        if nbp is None:
            payout_now = False
            print(f"  {C.GREY}next block production: unknown (daemon unreachable){C.RESET}")
        elif not nbp["scheduled"]:
            print(f"  {C.GREEN}{C.BOLD}[+]{C.RESET} {C.GREEN}No further blocks scheduled"
                  f"{C.RESET} {C.GREY}(daemon reports no won slots ahead){C.RESET}")
        elif nbp["epoch"] == staking_epoch:
            payout_now = False
            secs = nbp["seconds_from_now"] or 0
            when = datetime.datetime.utcfromtimestamp(
                nbp["start_time_ms"] / 1000).strftime("%Y-%m-%d %H:%M UTC")
            total_wait = secs + req * (report["sec_per_block"] or 210)
            print(f"  {C.YELLOW}{C.BOLD}[~]{C.RESET} {C.YELLOW}Another block is scheduled "
                  f"this epoch{C.RESET} - slot {nbp['slot']}, {when} "
                  f"(in {C.BOLD}{_fmt_eta(secs)}{C.RESET})")
            print(f"      {C.GREY}epoch settles {_fmt_eta(total_wait)} from now "
                  f"(that block + {req} confirmations){C.RESET}")
            print(f"      {C.GREY}(only the next slot is known - add --vrf to see "
                  f"the whole epoch){C.RESET}")
        else:
            when = datetime.datetime.utcfromtimestamp(
                nbp["start_time_ms"] / 1000).strftime("%Y-%m-%d %H:%M UTC")
            print(f"  {C.GREEN}{C.BOLD}[+]{C.RESET} {C.GREEN}No more blocks in epoch "
                  f"{staking_epoch}{C.RESET} - next is epoch {nbp['epoch']}, "
                  f"slot {nbp['slot']} {C.GREY}({when}){C.RESET}")

    # --- Live balance of the wallet that receives the coinbase --------------
    print(_hr())
    bal = GraphQL.getAccountBalance(payout_wallet)
    print(f"  {C.BOLD}payout wallet{C.RESET} {C.GREY}{payout_wallet}{C.RESET}")
    if bal is None:
        print(f"  {C.RED}balance unavailable{C.RESET} "
              f"{C.GREY}(daemon unreachable - check the SSH tunnel on 3085){C.RESET}")
    else:
        print(f"  balance {C.BOLD}{bal['total']:,.4f} MINA{C.RESET}")
        if bal["pending_txs"]:
            print(f"  {C.YELLOW}{C.BOLD}[!]{C.RESET} {C.YELLOW}{bal['pending_txs']} outbound "
                  f"tx(s) still in the mempool{C.RESET} "
                  f"{C.GREY}(nonce {bal['nonce']} -> {bal['inferred_nonce']}){C.RESET}")
        if b_list:
            unpaid = sum(b["coinbase_mina"] for b in b_list
                         if b["chain_status"] != "orphaned")
            print(f"  {C.GREY}of which ~{unpaid:,.0f} MINA is this epoch's coinbase, "
                  f"still owed to delegators{C.RESET}")

    # --- Verdict ------------------------------------------------------------
    print(_hr())
    if mature and payout_now:
        print(f"  {C.GREEN}{C.BOLD}READY{C.RESET} {C.GREEN}- epoch is settled, "
              f"safe to run:{C.RESET} "
              f"{C.BOLD}python3 send_payout.py --epoch {staking_epoch}{C.RESET}")
    elif mature and not payout_now:
        print(f"  {C.YELLOW}{C.BOLD}PARTIAL{C.RESET} {C.YELLOW}- payable now, but more "
              f"coinbase is still expected this epoch.{C.RESET}")
        print(f"  {C.GREY}Paying now means a second payout later for the same epoch.{C.RESET}")
    elif immature:
        print(f"  {C.YELLOW}{C.BOLD}WAIT{C.RESET} {C.YELLOW}- blocks found but not yet "
              f"confirmed.{C.RESET}")
    else:
        print(f"  {C.GREY}Nothing to pay out for this epoch.{C.RESET}")
    print(C.BOLD + C.CYAN + "=" * 74 + C.RESET + "\n")

# Initialize some stuff
total_staking_balance = 0
total_staking_balance_unlocked = 0
total_staking_balance_foundation = 0
total_staking_balance_labs = 0
all_block_rewards = 0
all_x2_block_rewards = 0
supercharged_rewards_by_foundation_and_labs = 0
total_snark_fee = 0
all_blocks_total_fees = 0
payouts         = []
blocks          = []
blocks_included = []
store_payout    = []

# Get the staking ledger for an epoch
try:
    staking_ledger = GraphQL.getStakingLedger({
        "delegate": public_key,
        "ledgerHash": ledger_hash,
    })
except Exception as e:
    print(e)
    exit("Issue getting staking ledger from GraphQL")

if not staking_ledger["data"]["stakes"]:
    exit("We have no stakers")

try:
    blocks = GraphQL.getBlocks({
        "creator":        public_key,
        "epoch":          staking_epoch,
        "blockHeightMin": min_height,
        "blockHeightMax": max_height,
    })
except Exception as e:
    print(e)
    exit("Issue getting blocks from GraphQL")

if not blocks["data"]["blocks"]:
    exit("Nothing to payout as we didn't win anything")

csv_header_delegates = "address;stake;delegation_type;;is_locked?are_tokens_locked?"
delegator_file_name  = "delegates.csv"
write_to_file(data_string=csv_header_delegates, file_name=delegator_file_name, mode="w")
latest_slot_for_created_block = blocks["data"]["blocks"][0]["protocolState"]["consensusState"]["slotSinceGenesis"]

for s in staking_ledger["data"]["stakes"]:
    # skip delegates with staking balance == 0
    if s["balance"] == 0:
        continue

    if not s["timing"]:
        # 100% unlocked
        timed_weighting = "unlocked"
        total_staking_balance_unlocked += s["balance"]
    elif s["timing"]["untimed_slot"] <= latest_slot_for_created_block:
        # if the last slot of the last created validator by the block >= untimed_slot,
        # then we consider that in this epoch the delegator tokens are completely unlocked
        timed_weighting = "unlocked"
        total_staking_balance_unlocked += s["balance"]
    else:
        # locked tokens
        timed_weighting = "locked"

    # Is this a O(1)Labs address
    if s["public_key"] in labs_delegations:
        delegation_type = 'o(1)labs'
        total_staking_balance_labs += s["balance"]

    # Is this a Foundation address
    elif s["public_key"] in foundation_delegations:
        delegation_type = 'foundation'
        total_staking_balance_foundation += s["balance"]
    else:
        delegation_type = 'common'

    payouts.append({
        "publicKey":             s["public_key"],
        "total_reward":          0,
        "staking_balance":       s["balance"],
        "percentage_of_total":   0,                     # delegator's share in %, relative to total_staking_balance
        "percentage_of_SP":      0,                     # percentage of unlocked tokens from the total amount of unlocked tokens
        "timed_weighting":       timed_weighting,
        "delegation_type":       delegation_type,
    })

    total_staking_balance += s["balance"]
    delegator_csv_string = f'{s["public_key"]};{float_to_string(s["balance"])};{delegation_type};{timed_weighting}'
    write_to_file(data_string=delegator_csv_string, file_name=delegator_file_name, mode="a")

csv_header_blocks = "block_height;slot;block_reward;snark_fee;tx_fee;epoch;state_hash"
blocks_file_name = f"blocks.csv"
write_to_file(data_string=csv_header_blocks, file_name=blocks_file_name, mode="w")

for b in reversed(blocks["data"]["blocks"]):
    if not b["transactions"]["coinbaseReceiverAccount"]:
        print(f"{b['blockHeight']} didn't have a coinbase so won it but no rewards.")
        continue

    if not b["canonical"]:
        print("Block not in canonical chain")
        continue

    winner_account = b["winnerAccount"]["publicKey"]
    block_height = b["blockHeight"]
    slot         = b["protocolState"]["consensusState"]["slotSinceGenesis"]
    block_reward_mina = int(b["transactions"]["coinbase"]) / decimal_
    block_reward_nano = int(b["transactions"]["coinbase"])
    snark_fee    = b["snarkFees"]
    epoch        = b["protocolState"]["consensusState"]["epoch"]
    state_hash   = b["stateHash"]
    tx_fees      = b["txFees"]

    total_snark_fee += int(snark_fee)
    all_blocks_total_fees += int(tx_fees)
    blocks_included.append(b['blockHeight'])

    # if supercharged block winning account in Foundation or o(1)labs
    # full supercharged reward to validator
    if block_reward_mina > COINBASE and \
            winner_account in foundation_delegations + labs_delegations:
        supercharged_rewards_by_foundation_and_labs += block_reward_nano - (COINBASE * decimal_)
        all_block_rewards += block_reward_nano - (COINBASE * decimal_)

    # if supercharged reward winning account not in Foundation or o(1)labs
    # split all SC rewards across unlocked wallets
    elif block_reward_mina > COINBASE and\
            winner_account not in foundation_delegations + labs_delegations:
        all_x2_block_rewards += block_reward_nano - (COINBASE * decimal_)
        all_block_rewards += block_reward_nano - (COINBASE * decimal_)

    # without SC rewards --> split rewards across all delegators
    else:
        all_block_rewards += block_reward_nano

    csv_string = f"{block_height};" \
                 f"{slot};" \
                 f"{block_reward_mina};" \
                 f"{float_to_string(int(snark_fee) / decimal_)};" \
                 f"{float_to_string(int(tx_fees) / decimal_)};" \
                 f"{epoch};" \
                 f"{state_hash};"

    write_to_file(data_string=csv_string, file_name=blocks_file_name, mode="a")

total_reward = all_block_rewards + all_blocks_total_fees - total_snark_fee
delegators_reward_sum = 0
payout_table = []

# --- payout redirects -------------------------------------------------------
# Configured in config.yml under PAYOUT_REDIRECTS; see redirects.py for the
# format. Nothing to edit here.
PAYOUT_REDIRECTS = redirects.parse(
    c.get("PAYOUT_REDIRECTS"),
    warn=lambda m: print(f"{C.RED}{m}{C.RESET}"),
)
if PAYOUT_REDIRECTS:
    print(f"\n{C.BOLD}Payout redirects active: {len(PAYOUT_REDIRECTS)}{C.RESET}")
    for line in redirects.describe(PAYOUT_REDIRECTS):
        print(f"  {C.GREY if not line.startswith(' ') else C.CYAN}{line}{C.RESET}")
    print()

# destination address -> merged payout
payout_rows = {}

for p in payouts:
    if p["delegation_type"] == 'foundation':
        p["percentage_of_total"] = float(p["staking_balance"]) / total_staking_balance
        p["total_reward"] = float(total_reward * p["percentage_of_total"] * (1 - foundation_fee))

    elif p["delegation_type"] == 'o(1)labs':
        p["percentage_of_total"] = float(p["staking_balance"]) / total_staking_balance
        p["total_reward"] = float(total_reward * p["percentage_of_total"] * (1 - labs_fee))

    elif p["timed_weighting"] == "unlocked":
        p["percentage_of_SP"] = float(p["staking_balance"]) / total_staking_balance_unlocked
        p["percentage_of_total"] = float(p["staking_balance"]) / total_staking_balance
        p["total_reward"]        = float(total_reward * p["percentage_of_total"] * (1 - fee))
        p["total_reward"] = p["total_reward"] + (float(all_x2_block_rewards * p["percentage_of_SP"] * (1 - SP_FEE)))

    else:
        p["percentage_of_total"] = float(p["staking_balance"]) / total_staking_balance
        p["total_reward"]        = float(total_reward * p["percentage_of_total"] * (1 - fee))

    delegators_reward_sum += p["total_reward"]


    payout_table.append([
        p["publicKey"],
        p["staking_balance"],
        float_to_string(p["total_reward"] / decimal_),
        p["delegation_type"],
        p["timed_weighting"]
    ])

    # Where does this reward actually go? Without a redirect: to the staking
    # address itself. With one: to one or more configured destinations.
    targets = PAYOUT_REDIRECTS.get(p["publicKey"]) or [(p["publicKey"], 1.0)]

    for dst, amount in redirects.split(p["total_reward"], targets):
        payout_rows.setdefault(dst, {"nano": 0.0,
                                     "delegation_type": p["delegation_type"],
                                     "timed_weighting": p["timed_weighting"],
                                     "sources": []})
        payout_rows[dst]["nano"] += amount
        payout_rows[dst]["sources"].append(
            p["publicKey"] if len(targets) == 1
            else f'{p["publicKey"]} ({[f for d, f in targets if d == dst][0]*100:g}%)')

# Write the payout file, one line per DESTINATION address. Without redirects
# this is one line per delegator, exactly as before; with redirects the merged
# amounts are written instead.
# The minimum is applied to the FINAL transfer, after redirects are merged:
# a delegator below the threshold on their own may still clear it once their
# share is combined with others going to the same destination.
for dest, row in payout_rows.items():
    if row["nano"] / decimal_ < MINIMUM_PAYOUT:
        continue
    payout_string = f'{dest};' \
                    f'{float_to_string(int(row["nano"]))};' \
                    f'{float_to_string(row["nano"] / decimal_)};' \
                    f'{row["delegation_type"]};' \
                    f'{row["timed_weighting"]}'
    write_to_file(data_string=payout_string,
                  file_name=f'e{staking_epoch}_payouts.csv', mode='a')

redirected = [(d, r) for d, r in payout_rows.items()
              if r["sources"] != [d]]
if redirected:
    print(f"\n{C.BOLD}Redirected payouts{C.RESET}")
    for dest, row in redirected:
        print(f"  {C.CYAN}{dest}{C.RESET}  <- "
              f"{float_to_string(row['nano'] / decimal_)} MINA from "
              f"{len(row['sources'])} address(es)")
        for src in row["sources"]:
            print(f"      {C.GREY}{src}{C.RESET}")

# pprint(payouts)
# We now know the total pool staking balance with total_staking_balance
print(f"The pool total staking balance is:    {total_staking_balance}\n"
      f"The Foundation delegation balance is: {total_staking_balance_foundation}\n"
      f"O(1)Labs delegation balance is:       {total_staking_balance_labs}\n"
      f"Blocks won:                           {len(blocks_included)}\n"
      f"Delegates in the pool:                {len(payouts)}")

validator_reward = total_reward + all_x2_block_rewards +\
                   supercharged_rewards_by_foundation_and_labs - delegators_reward_sum

print(f'Foundation + O(1)Labs supercharged rewards: {supercharged_rewards_by_foundation_and_labs / decimal_}')
print(f'Supercharged rewards total: {all_x2_block_rewards / decimal_}')
print(f'Total:                      {(total_reward + all_x2_block_rewards) / decimal_}')
print(f'Validator fee:              {validator_reward / decimal_}')

print(tabulate(payout_table,
               headers=["PublicKey", "Staking Balance", "Payout mina",
                        "Delegation_type", "Tokens_lock_status"], tablefmt="pretty"))
