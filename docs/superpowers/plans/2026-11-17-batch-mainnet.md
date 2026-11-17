# Mainnet batch payments implementation plan

**Goal:** Real multi-recipient mainnet payments with full archive proof, existing-wallet signing and separate durable accounting, preserving all existing payout plans.

**Spec:** User requests mainnet completion, then a reviewed micro-test to the same 24 recipients as the immutable epoch 9 plan, exactly 1 nanomina each, memo `batch_test`. Show amounts and fee before submission. Configurable batch splitting must handle more recipients.

**Architecture:** Python orchestration holds the existing wallet lock. An independent SQLite batch journal under the private state directory records immutable plans, one nonce/hash per batch and per-recipient allocations. Existing payout journal rows are never rewritten; normal workflows consult batch reservations/debits and fresh proofs through an optional compatibility bridge. Pinned o1js builds and signs transactions; credentials pass through pipes only, never command arguments or stored plaintext.

## Interfaces and tasks

- [x] `batch_archive.py`: `verify(config, expected, required_depth=3)` returns a complete exact inclusion proof or pending/unknown; read-only PostgreSQL, capture authoritative producer chain via preflight and expand ancestry beyond 290 blocks. Expected packet: wallet, network, nonce, tx_hash, fee_nano, memo and ordered recipients. Reject orphan/fork, duplicate, wrong payer/token/amount or hidden updates. Tests with realistic SQL-shaped fixtures.
- [x] `batch_signer.py` and `batch-pay/mainnet-worker.mjs`: build unsigned transaction, sign via existing encrypted Mina key with password provided interactively, verify signatures and full immutable expected payload before returning signed JSON/hash. No production signing or key access during implementation tests. Mock subprocess boundaries plus real o1js signature tests.
- [x] `batch_accounting.py`: separate durable journal, atomic nonce claims, immutable payload, uncertain-before-send, confirmed/failed proof accounting. Existing owner summary includes external debits/reservations; nonce guard recognizes only known batches. New module exposes read-only snapshots to legacy workflows; current journal unchanged. Shared wallet lock. Independent conservation/restart/fault tests.
- [x] `batch_mainnet.py`: prepare/review/send/resume/status CLI using the modules above, explicit fee/size/memo, prior ordinary payments verified, owner-budget and carry reserves protected. Only one HTTP submission attempt. Sender bound to config; no account creation for micro-test. Handle multiple batches serially with durable recovery.
- [x] Independent reviews, full Python and Node suites, archive live read-only proof checks, privacy check.
- [x] Prepare the exact 24-recipient micro-test and show total + fee + spendable funds. Only then proceed with the explicitly requested test if signing/funding is available; never touch existing payout reservations or resend uncertain batches.

## Safety and review focus

- Known batch nonces must not be mistaken for unknown outgoing ordinary transfers.
- Owner income and wallet liquid funds each debit actual total once; carry reserves remain protected.
- Pending mainnet batch blocks conflicting normal operations through shared locking and reservations.
- Historical batch proofs must remain available beyond the daemon window.
- A failed included zkApp consumes fee/nonce but pays no recipients; no automatic retry or fee refund.
- Tests must cover signed-content tampering, wrong network, duplicate claims, two-process/restart recovery and split-boundary behavior.

Mainnet test execution is not part of automated test commands. Production passwords/private keys never appear in logs, source, command arguments, reports or Git.

## Verification outcome — 2026-11-17

- Full checkout: 347 Python tests passed, no skips. Public-only copy: 347 run, 19 expected private-module skips, no failures. Node: 34 passed.
- Independent core/CLI review caught and fixed string/integer era drift, nested read-write journal opens, unsigned suffix reservation after failure, and stale batch proofs across interactive signing. Added regression tests for each, including persisted-plan → archive → settlement.
- Historical reconciliation tolerates a current protocol upgrade while new signatures require the current era. Password handling refuses getpass visible-input fallback.
- Actual unsigned mainnet preview: 24 existing recipients copied from the saved epoch-9 plan, 1 nanomina each, memo `batch_test`, one batch, fee 6000001 nanomina, total 6000025 nanomina. Real o1js limits passed.
- Ordinary journal, carry ledger and payout artifacts were byte-identical before/after preview. No mainnet batch journal exists yet. No production signing/key access/submission performed.
- At preview time the micro-test required funding and interactive password entry. The separate private proposal was ready; wallet balances remain in private operational records.

## Mainnet test completed — 2026-11-17

The user explicitly authorized a temporary reserve advance of 6000025 nanomina
for this test and entered the wallet password locally. The single 24-recipient
transaction was included with Applied status, reached depth 3, and was reconciled
through the real archive into the separate batch journal. Costs: 24 nanomina to
recipients plus 6000001 fee; batch reserve is now zero. Ordinary payout files and
journal remained byte-identical during reconciliation, and delegator liabilities
were not reduced. A private proof report and verified backup were saved.

The live run exposed a transient archive-ingest race in batch proof reads. Added
bounded read-only retry for PreflightArchiveLagError, with no retries for identity
mismatches or submission. Final Python suite: 351 passed, no skips.
