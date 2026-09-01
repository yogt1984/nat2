# TERMINAL_COMMAND_0 — `nat2 liqview`

One terminal command that answers, by eye: **did price move toward the
liquidation cluster, and was the map asymmetric before it did?**

That question is the magnet hypothesis. This command does not test it — `gate
magnet` does, under a pre-registration. This is the instrument you look through
*before* deciding what to pre-register, and the one you reach for at 3 a.m. when
something moved and you want to know whether the map saw it coming.

One file per unit of work, in execution order. Each states **what**, **how**,
**how it is verified**, and a single **done when**.

## Status legend
`todo` · `doing` · `done` · `blocked`

## Order

| # | Task | Status |
|---|------|--------|
| 00 | Decisions and scope | todo |
| 01 | The data slice | **done** |
| 02 | Two views, and why the absolute one is the point | **done** |
| 03 | The ASCII heatmap | **done** |
| 04 | The price path and the asymmetry strip | **done** |
| 05 | Realized liquidations overlay | **done** |
| 06 | The CLI surface | partial |
| 07 | Remote: check and fetch from the Hetzner box | todo |
| 08 | Tests and verification | **done** |

## Rules that apply to every task
- Task branch `feat/terminal-command-0`, conventional commits, `merge --no-ff`.
  Never on `main`.
- **Read-only against the store.** This command renders; it never writes into
  `data/raw`, never appends to the ledger, never records a verdict.
- **Stdlib-only, on `/usr/bin/python3`.** It joins `gapwatch`, `statuspage` and
  `tapecheck` as an ops tool that must survive a broken venv — the venv being
  broken is one of the moments you most want to look at the tape.
- **Every number carries what qualifies it.** Coverage is ~38–42%, so every
  panel prints it. A heatmap that does not say it is drawn from 38% of open
  interest is a picture that lies by omission.
- No new threshold in code. Anything that gates a decision is pre-registered on
  the ledger first; this tool has no thresholds because it decides nothing.
