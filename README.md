# nat2

A terminal research and execution engine for **Hyperliquid only**.

HL is the one venue where the liquidation map and the trader population are exact data rather
than reconstructions. This system exists to find out whether that exactness is worth anything —
the thesis under test is that clustered liquidations act as a magnet: price is drawn toward dense
forced-flow zones, then overshoots and snaps back.

## Two rules

**Gates before models.** Each gate is a CLI command that writes a PASS/FAIL verdict to a
hash-chained ledger, and every downstream command refuses to run when its upstream gate is
missing or FAIL. The refusal is the product.

| Gate | Question | Kills |
|---|---|---|
| `feed` | is the data intact and causal? | everything |
| `map` | does the map predict HL's *own realized* liquidations, at adequate coverage? | the map branch |
| `magnet` | does the cluster pull, net of costs, better than `sign(imb)`? | magnet experts |
| `persistence` | does wallet skill in `[t−90d, t]` predict `(t, t+30d]`? | the smart-money branch |
| `decay` | does cohort-flow alpha survive past minutes? | copyability |

**Exact or nothing.** No estimated maps, no assumed leverage mixes, no aggregator history. Where
a quantity can't be had exactly, don't compute it — the approximation is what makes a backtest
lie. Every map carries its coverage number; a map without one is a lie with a chart on it.

## Quickstart

```bash
uv sync --extra dev
uv run pytest

uv run nat2 capture hl --all        # WORM ingest, dual-timestamped — start this first
uv run nat2 gate feed               # is the data intact and causal?
uv run nat2 wallets seed            # leaderboard-seeded registry (equity + volume)
uv run nat2 cycle                   # snapshot, then observe liquidations
uv run nat2 map BTC --rank          # liquidation map, coverage, and which cluster ranks
uv run nat2 gate map                # and whether it earns the right to be built on
uv run nat2 log verify              # the ledger is tamper-evident; check it
```

Capture accrues calendar time. Point-in-time series cannot be recovered later, so the daemon runs
while the rest is built.

## Layout

```
src/nat2/
  hl/         ws · info · leaderboard · ratelimit · schemas   ← only package that knows HL exists
  io/         worm · capture · compact · snapshot · liqscan · cycle
  core/       clock · registry · schedule · guard
  features/   liqmap · liqmath · liquidations · fills
  gates/      feed · map
  validate/   audit_feed
  ledger/     chain
```

Layers only call downward — that is what lets the simulator and the live path share code honestly.
Storage is append-only `ndjson.zst` (WORM) → Parquet; SQLite holds mutable state only. A closed
file is checksummed into a manifest and never reopened, so a changed digest always means
corruption and never merely a bounced daemon.

Python 3.12 + uv · polars · typer · rich · websockets/httpx · pydantic.

## Status

M0 done (`gate feed` PASS). M1 built; `gate map` FAILs on `predictive` — a gate that cannot yet
be evaluated must not pass. The open question is whether the wallets we map and the wallets that
actually get liquidated are the same population; `nat2 cycle` is accumulating the series that
settles it.

**Capture has been running continuously since 2026-08-13** under
[`packaging/systemd`](packaging/systemd/) — 18 coins, recycled every 6h around an unexplained
long-run degradation. That is day 1, and every date in `HYPOTHESIS_1.md` §8 is measured from it.

- [`ASKING.md`](ASKING.md) — how to read the liquidation map: what to ask, which flags are not
  cosmetic, and the five ways to get a plausible wrong answer.
- [`docs/DESIGN.md`](docs/DESIGN.md) — the full design: capture, features, experts, meta,
  allocation, execution, milestones, and the risks worth naming.
- [`TASKS.md`](TASKS.md) — what is next, what it unblocks, and what is still unverified.
- [`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) — the magnet claim, pre-registered: null, controls and
  decision rule fixed before the data. The concrete form of `gate magnet`.
- [`CONSISTENCY.md`](CONSISTENCY.md) — how the drift field is estimated, and the cross-cell
  constraint that supplies the power the sample size does not.
- [`ATTACK.md`](ATTACK.md) — the function itself: why a cluster is worth pushing into, and why
  its exponents are forced by the impact law rather than fitted.
