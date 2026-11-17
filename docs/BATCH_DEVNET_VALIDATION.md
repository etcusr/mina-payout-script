# Batch pay: Devnet qualification, 2026-11-17

## Result

Real multi-recipient zkApp payments succeeded on public Mina Devnet.
Both recipients received exactly **1 nanomina** in a single transaction.
The loss-of-response test used one network POST, restarted in a new process,
then reconciled the original hash without any duplicate submission.

| Check | Evidence |
|---|---|
| Initial two-account setup batch | [Transaction](https://minascan.io/devnet/tx/5JumPycd28CSdnJZdCw5k2Epr8EmfFKFe2HAYnF3btD9FAwPmAGv?type=zk-tx), nonce 0, confirmed at 3 descendants |
| Two existing-account micro payments | [Transaction](https://minascan.io/devnet/tx/5JtfvAmwjabCM5rnXXschsr9raxvm5CJLkCwaEgnejNJwKwCRfCk?type=zk-tx), nonce 1 |
| Inclusion | Applied, height 590853 |
| Verification time | 2026-11-17 06:12:29 UTC |
| Descendants at verification | 3 |
| Recipient balances | Each changed from 1 to 2 nanomina |
| Sender change for micro batch | 100000002 nanomina: 2 sent + one 100000000 fee |
| HTTP submissions | 1 |
| Restart/repeat submissions | 0 |

The transport fault was injected **after the real HTTP POST completed**, by
discarding its response and raising a connection error. The journal persisted
`unknown` before the request. A new process found the same hash in the mempool,
then in the chain. This tests ambiguous submission recovery; it was not a
physical SSH disconnection. Loss before acceptance is covered by local tests
and deliberately remains unresolved rather than being automatically resent.

Private fixture keys, journal and exact balance evidence remain under
`batch-pay/.state/devnet/`, excluded from Git. Production keys, payout journals
and existing payout plans were not used for these tests.

## Automated checks

- 28 Node tests: real LocalBlockchain signatures and balance changes, 30 existing
  and 30 new recipients, signature tampering, segment limits, atomic recipient
  failure, nonce consumption, split batches, interruption before/after acceptance,
  concurrent submission claim, restart, chain identity and reorg handling.
- 279 Python tests passed in the working checkout.
- The public source copy also passed 279 Python tests, with 19 expected skips
  for optional private modules/configuration.
- npm audit: no reported vulnerabilities in the pinned dependency set.
- Node v26.13.0, npm 11.20.0, o1js 3.1.0. Independent review findings were fixed
  and covered with regression tests.

## Qualification boundary

This remains a **Devnet-only CLI**, not a production payout switch. The separate
[mainnet implementation](BATCH_MAINNET.md) now provides archive-based zkApp
proofs beyond the 290-block daemon window, production signing, and integration
with the existing wallet lock, nonce history and separate batch accounting.
The test does not authorize copying a production private key into these fixtures.
The test fee of 0.1 tMINA is not a claim about the minimum mainnet fee.
