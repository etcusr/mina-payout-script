# Integrated Epoch Batch Implementation Plan

> Execute with fresh task implementers and independent review; user has authorized implementation and delegated routine decisions. No commit/push or production transfers.

**Goal:** Normal aliases calculate, send, resume and settle future epochs as true multi-recipient batches.
**Architecture:** Flat immutable payout plans + deterministically linked batch journal + validated projected recipient receipts. Existing settled records are unchanged.
**Tech Stack:** Python 3.11, SQLite, existing PostgreSQL proof adapter, pinned o1js.
**Spec:** docs/superpowers/specs/2026-11-17-integrated-batch-design.md

## Tasks
- [x] Calculation: policy transport/size/batch fee, exact equal per-batch fee shares, minimum/carry fixed point; tests for 65 recipients, dust, validator mode and old behavior.
- [x] State bridge: deterministic linked batch plan and validated virtual recipient intents; prevent ordinary reservation of batch plan; owner costs only once; pending reservations and history proofs; tests for failures, tampering, crash gap and settlement.
- [x] Workflow: create frozen batch policy/recipient fees; send/reconcile through existing aliases with fresh proof/funding/epoch guards; one password prompt, sequential/resumable execution, terminal failure settlement; full fake end-to-end tests.
- [x] Presentation: balance and send tables show per-batch costs/count/progress, no repeated whole table, full addresses and links; docs/config example.
- [x] Independent review, full Python/Node/public-copy checks and readonly historical plan verification. Enable batch in private config only after passing, preserve ordinary journal/CSV/carry hashes.

## Review focus
- Cross-journal link cannot be forged to suppress owner debits or skip delegator settlement.
- Per-recipient fee shares sum exactly to executed fees, including failed and cancelled suffix.
- Restart after accepted-but-lost response cannot duplicate, even with config change.
- Reward ancestry and previous applied proofs fresh after interactive password.
- Empty epochs, sub-threshold accounts, new-account refusal, missing linked journal and older protocol eras.

## Completion and verification

- Integrated normal wrappers: calc_rewards.py → plan, send_payout.py → send --complete. Existing saved epochs retain their immutable transport and fee policy.
- 404 Python tests passed; 34 Node tests passed. Public-only copy: 404 run, 19 expected private-module skips, no failures.
- Independent review fixed post-submission linked-journal loss with a durable ordinary-journal marker, prepared last-packet resume, and pending-batch balance reports. Marker is persisted before signing; nonce must equal initial nonce + batch index. No blind recreation after journal loss.
- CLI integration tests cover one confirmation/password, all packets, confirmation waits, backup, epoch settlement, owner withdrawal and idempotent repeated invocation. Also tested loss before/after acceptance, config changes, failed fees/cancelled suffix, readonly reconciliation, empty epochs and protected carry.
- Private configuration enables zkapp_batch, batch size 30, fee 6000001 nanomina. Library/example default remains ordinary payment. Only these three private configuration values changed; existing journal, batch journal, carry ledger and payout CSV hashes unchanged.
- No production transfers or new epoch plans were created. The next real epoch payout remains the user's later operational test. No commit, push or release performed.
