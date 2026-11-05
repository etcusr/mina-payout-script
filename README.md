# mina-payout-script

**English** · [Русский](README.ru.md)

Exact reward calculation and recoverable Mina payouts using your producer and archive PostgreSQL. Payments are authorized by immutable plans and a durable SQLite journal. Legacy CSV files are historical evidence, never a retry queue.

## Setup

Python 3.9+, the dependencies in `requirements.txt`, a complete Mina archive, a synced producer with the payout key, and SSH access are required. `./scripts/install.sh` creates a virtualenv and copies `config.example.yml` without overwriting an existing configuration. Configure your addresses, monetary policy, `EXPECTED_CHAIN_ID`, actual remote GraphQL port, SSH alias, and a **SELECT-only** archive connection. The config remains private.

```bash
./scripts/tunnel.sh --ensure
python3 payout_cli.py doctor --epoch 8 --wallet
python3 payout_cli.py init                  # genuinely new installation only
# Existing local history: set LEGACY_PROTOCOL_VERSION_ID explicitly, then:
python3 payout_cli.py init --import-legacy
```

Initialization is explicit and refuses to replace a journal. Import validates original receipt/CSV files, records their hashes and era, and leaves them and the optional private ledger unchanged. Existing submitted or reconciled epochs cannot be sent again. Missing/corrupt state fails closed. Do not initialize a second independent state directory to bypass a pending operation.

## Normal workflow

```bash
mina_calc --epoch 7
mina_pay --epoch 7
mina_withdraw                       # withdraw the recorded income balance
```

The existing aliases call the updated entrypoints: tunnel/health checks,
pre-operation backup, guarded submission, confirmation wait, accounting
settlement and post-operation backup are automatic. Already-loaded functions
pick this up without re-sourcing. Otherwise, load `source ./scripts/aliases.sh`
from the project root. `send_payout.py` and `withdraw.py` enable `--complete`;
low-level `payout_cli.py send/withdraw` retain manual behavior without that flag.

The automatic wait defaults to 3600 seconds (`AUTO_SETTLE_WAIT_SECONDS` or
`--wait-seconds`). Timeout exits 2 as pending; repeat the same command later.
Ctrl+C exits 130 with resumable state and an operation-bound continuation. After an explicit --new-operation, resume using the printed command; never repeat that flag. A matching pending withdrawal is reused
without copying a UUID; a matching settled withdrawal is not sent again.
Use `--new-operation` explicitly for another withdrawal of the same amount.
Manual doctor/status/reconcile and explicit plan/operation IDs remain available.


Substitute an actual closed epoch. The current epoch produces a **read-only preview**. `--preview` also allows a separate calculation of a new epoch without creating a plan or CSV. Legacy paid epochs return their original read-only report; historical commission is never guessed from today's config. Existing plans return their frozen calculation. Changed settings do not alter an existing plan. `COMMISSION_SCHEDULE` supports era/epoch effective rates for future plans.

Calculation records staking inputs, their era-qualified archive ledger hash, block hashes, exact effective policy, carry and gross/net amounts. Each plan is identified by its SHA256 digest in `.payout-state/journal.sqlite3` and a private `<digest>.plan.json` view. `era<ERA>_e<EPOCH>_owed.csv` and `..._payouts.csv` are compatibility exports; `e<EPOCH>_*.csv` are created only when absent. The sender uses the journal plan, never edited CSV contents. All paths default to the project directory, regardless of the caller's working directory. Explicit `--project-root`, `--config`, and `--state-dir` select an isolated installation.

Before signing, the sender holds the shared wallet lock, validates fresh producer/archive/account evidence, checks frozen reward blocks, reserves known obligations and outgoing commitments, asks for confirmation/password, and refreshes all evidence again. It durably records an intent and **explicit nonce before sendPayment**. Wallet locking is attempted in `finally`, including an unlock timeout. Passwords are not saved.

```bash
python3 send_payout.py --plan DIGEST --resume
python3 payout_cli.py reconcile --plan DIGEST          # read-only proof preview
python3 payout_cli.py reconcile --plan DIGEST --commit
```

A timeout, malformed response, lost receipt or local-write failure leaves a blocking uncertain intent. Resume searches the exact sender, nonce, recipient, amount, fee and decoded memo on the captured best chain/mempool. It does **not** resubmit that payment. Missing transactions and expiration do not prove failure. Only a different successfully applied transaction at the same nonce with the required depth can establish failure. Unknown outcomes require investigation, not deletion/reset of state.

Outgoing payments settle after `applied` on the producer best chain plus **3 descendants** (`TX_CONFIRMATIONS_NUM`). Before calculating or sending a later payout epoch, previous payments are freshly reverified with **15 descendants** (`PREVIOUS_PAYOUT_CONFIRMATIONS_NUM`); verification repeats after the password prompt, before unlock. Withdrawals recheck previous operations at the ordinary outgoing threshold. Exact sender, nonce, recipient, amount, fee, memo, hash, network and ancestry must match. Imported historical payments without verifiable receipts block further sends. Reward-block maturity (`CONFIRMATIONS_NUM`) remains separate.

An explicit `TX_CONFIRMATIONS_NUM` also applies to existing plans without modifying their immutable payment data or digest; the actual threshold is recorded in confirmation evidence. New plans default to 3; existing plans without an explicit override retain their recorded threshold. Running processes retain their loaded settings. The human screen displays `Applied` with actual/required depth for each payment, full MinaScan account links, and semantic status colors. Redirected output stays plain; `NO_COLOR=1` disables colors, `NO_HYPERLINK=1` disables terminal links, and `MINA_DEBUG=1` enables schema diagnostics.

A timeout remains resumable pending; `--wait-seconds 0` skips waiting. Reconciliation rechecks exact proofs, is idempotent, and carries unpaid amounts. Empty/dust-only plans must also settle to preserve credits. Reorgs block further action; settled history is never automatically undone. `--force`, `--accept-pending`, and CSV-based financial commits remain refused.

If node connectivity is lost after submission, the confirmation loop checks the tunnel and retries fresh reconciliation within the existing wait deadline. It never resubmits payments. An outage at the deadline is reported as unknown confirmation status, with the journal and reservation retained. Network identity, archive integrity and proof errors still stop immediately. An unavailable SSH connection at command startup must be restored before resuming the original command.

## Exact accounting and withdrawals

Without `--amount`, `mina_withdraw` uses recorded validator commission plus retained rewards of configured own addresses, less payout fees, prior withdrawals, reservations and the new withdrawal fee. Rewards already paid to own addresses as delegators are shown separately and never included twice. The destination comes from `WITHDRAW_TO_ADDRESS` in `config.yml`. Use `mina_withdraw --dry-run` to preview without sending.

Successful reconciliation ends with a fresh database summary: withdrawn including fees, remaining before the next withdrawal fee, and reserved including fees. Repeating the command with no new income also displays this summary.

This wallet's initial income baseline covers epochs 6 and 7; earlier transfers are not retroactively assigned to those epochs. Later settled payouts add one immutable credit per epoch. A durable withdrawal reservation precedes signing, so retries resume the same operation and confirmed withdrawals reduce the remaining income. Repeating the command with no new income sends nothing. Untracked outgoing nonces after the baseline block automatic withdrawal. Journal schema 3 and backups include income credits and withdrawals. Explicit `--amount` remains available and is also debited; automatic mode refuses to implicitly resume a pending manual withdrawal.

Rates and staking balances parse directly as Decimal; allocation uses exact rational arithmetic. Vesting parameters must be complete, finite and nonnegative; final unlock uses ceiling division, zero-period vesting occurs at the cliff, and a zero increment with a positive remainder never unlocks within the protocol slot range. Each staking recipient's allocation is floored to nanomina, and the validator retains the residual. Redirect destinations are sorted; the last receives the integer remainder. Merged destinations then apply own-address exclusions, signed carry, and minimum payment. Positive carry means the pool owes the recipient; negative means previous overpayment. Gross entitlement, excluded own share, validator share and paid/carry totals are preserved exactly, including supercharged rules.

`PAYOUT_REDIRECTS` accepts a destination address or a mapping of destinations to positive weights. Invalid addresses, NaN/negative amounts, malformed receipts, duplicate destinations, and invalid redirects fail closed. Old CSV nanomina columns are authoritative; their historical display column may differ by one nanomina. New exports agree exactly.

```bash
python3 withdraw.py --amount 12.345678901 --to B62... --dry-run
python3 withdraw.py --amount 12.345678901 --to B62...
# Resume the SAME withdrawal using the UUID printed before password entry:
python3 withdraw.py --amount 12.345678901 --to B62... --operation-id UUID
python3 payout_cli.py reconcile --plan WITHDRAWAL_DIGEST --commit
```

Withdrawal uses the same journal, wallet lock, fresh checks, nonce and lock-finally rules. Any unresolved/unreconciled payout plan or legacy payout artifact blocks it. Positive carried obligations and the union of local/pool outgoing commitments are reserved. The requested amount must be exact. This reserve covers recorded obligations, not rewards that have not yet been calculated/planned; calculate and settle outstanding closed epochs before withdrawing commission. Dry-run never unlocks, submits or creates a withdrawal plan.

The optional gitignored `ledger.py`/`payout_ledger.json` preserve existing private history. Public-only installations use journal carry and `payout_cli.py reconcile`; private `reconcile.py` is only a compatibility wrapper. If an imported private ledger disappears, accounting stops. Private ledger balances and complete history are checked against the immutable initialization snapshot and ordered journal settlements. Restoring only an older ledger, deleting a digest entry, or silently editing carry stops signing/accounting. Private ledger save followed by journal failure recovers only through reconciliation of that exact pending plan after fresh proof; other operations stop during the gap, and the delta is never applied twice.

## Status, backup and recovery

```bash
python3 payout_cli.py status                          # offline, no initialization
python3 payout_cli.py status --epoch 6                # original legacy baseline
python3 payout_cli.py status --epoch 6 --online       # exact current chain proof
mkdir -p ./.payout-state/backups ./.payout-state/restore-checks
python3 payout_cli.py backup --destination ./.payout-state/backups/new-name
python3 payout_cli.py restore --backup ./.payout-state/backups/new-name --destination ./.payout-state/restore-checks/new-name
```

Backup locks the wallet and captures the SQLite journal, existing CSV, config and optional ledger with checksums and private permissions. Restore verifies all hashes and schema before creating a **new, absent destination**; it never replaces live state. Keep backups private because they contain credentials and financial records. Inspect a restored installation with explicit `--project-root DEST --state-dir DEST --config DEST/config.yml`; switching operating state is a deliberate operator action. Do not run old and restored installations concurrently.

The integrated journal uses schema **3**. Existing schema 2 journals upgrade automatically when opened; preserve a verified backup before upgrading. There is no automatic in-place migration from experimental schema 1: preserve its backup and resolve any existing intents before a reviewed migration. Normal upgrade from the legacy script uses `init --import-legacy`; it changes no legacy files. Rollback of code must not run the old unsafe sender against submitted new plans. Preserve journal/ledger/CSV, inspect status, and restore only into a new directory. Unreconciled historical legacy records without established accounting require a separately reviewed migration; there is no guessed paid/unpaid import.

## Health, tunnels and scope

Machine-readable status and `plan --json` write one JSON document to stdout; diagnostics and download/VRF progress go to stderr.

`doctor` checks configured producer/chain identity, SYNCED, canonical continuity and parent links, duplicate/stale classifications, captured best-chain ancestry, protocol era, optional wallet nonces/balance/pool, and bounded canonical lag. Every financial operation refreshes these checks. Reward and payment queries use the captured ancestry. Staking ledger identity is selected from archive `epoch_data`, never by newest bucket timestamp.

These checks do not replay Mina consensus or the ledger, prove completeness of every internal/user command, authenticate the full downloaded staking-ledger Merkle root, or replace PostgreSQL/storage corruption monitoring. Valid zero-coinbase blocks are supported; malformed/missing required fields or disagreement between reward and producer-block queries stops calculation. Calculation fingerprints record the actual downloaded inputs.

`calc.sh` and `mina_calc`/`balance_mina` ensure both forwarded services before calculations, including an explicit epoch. Healthy manual tunnels are reused. Only absent forwards are created; occupied wrong ports fail. `--stop` affects only the managed SSH control connection. Environment port overrides and `MINA_SSH_HOST` propagate through the shared exact config loader. No shell startup edits are needed beyond sourcing `scripts/aliases.sh`.

Existing `--vrf` and `--vrf-refresh` flags retain explicit VRF cache/scan behavior; scans may request the block producer key password and take minutes. They are separate from payment authority. Archive deployment instructions remain in [archive/README.md](archive/README.md). Keys/config, runtime state, CSV, private ledger/reconcile and `CLAUDE.md` remain gitignored.

## Verification

```bash
venv/bin/python3 -m unittest discover -v
python3 -m compileall -q . -x '/(venv|\.venv|archive/cache|mina)/'
```

Financial tests use fake transports and temporary directories. Public-only tests skip the optional private-ledger cases. Live validation should use `doctor` and read-only `status`; do not use a real payment to test deployment.
