# Hetzner cutover — task plan

One file per unit of work, in execution order. Each states **what**, **how**,
**how it is verified**, and a single **done when** condition.

Sources: the audited assessment, runbook and implementation specs
(three artifacts, 2026-08-29). Where a file quotes a number, it was measured on
2026-08-28/29 — re-derive rather than re-quote if the date has moved.

## Status legend
`todo` · `doing` · `done` · `blocked`

## Order

| # | Task | Status |
|---|------|--------|
| 00 | Decisions and preflight | todo |
| 01 | Register the Hetzner account | todo |
| 02 | Order server, volume, firewall | todo |
| 03 | cloud-init and first boot | todo |
| 04 | Harden the box | todo |
| 05 | Mount the volume | todo |
| 06 | Install nat2 | todo |
| 07 | Secrets and ntfy rotation | todo |
| 08 | Test blast shield (T6) | **done** |
| 09 | Ledger lock, registry WAL (T4) | **done** |
| 10 | tapecheck (T7) | **done** |
| 11 | Capture startup retry (T1) | **done** |
| 12 | Capture shared budget (T2) | **done** |
| 13 | Capture disk-full (T3) | **done** |
| 14 | Backup code (T13) | **code done** |
| 15 | Storage Box and restic | todo |
| 16 | Caddy and DNS | todo |
| 17 | External dead-man | todo |
| 18 | **The cutover** | todo |
| 19 | gapwatch honesty (T5) | **done** |
| 20 | Clean-days and pre-registration (T8) | todo |
| 21 | Testing agents (T12) | todo |
| 22 | The seven-day count | todo |
| 23 | nat ops interpreter (T15) | todo |
| 24 | nat Rust reconnect (T14) | todo |
| 25 | Liquidation-side decision (T9 vs T10) | todo |
| 26 | Liquidation statistics (T11) | todo |
| 27 | hl.ops and the clock fix (T16) | todo |

## Rules that apply to every task
- Task branch, conventional commit, `merge --no-ff`. Never on `main`.
- Planted test before real data; real-data smoke before commit.
- No new threshold in code — register it on the ledger first.
- Nothing restarts a unit on su-35. Unit changes are rendered as diffs.
