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
| `magnet` | does the cluster pull, net of costs, better than `sign(imb)` — and does a distance kernel beat no kernel? | magnet experts |
| `persistence` | does wallet skill in `[t−90d, t]` predict `(t, t+30d]`? | the smart-money branch |
| `decay` | does cohort-flow alpha survive past minutes? | copyability |

**Exact or nothing.** No estimated maps, no assumed leverage mixes, no aggregator history. Where
a quantity can't be had exactly, don't compute it — the approximation is what makes a backtest
lie. Every map carries its coverage number; a map without one is a lie with a chart on it.

Two rules follow from those and are enforced rather than documented: **pre-registration precedes
scoring code** (thresholds, grids and even procedural changes to the evaluator are appended to the
ledger before the data that would tempt them is seen), and **nothing here sends an order** —
actions are observed, logged and simulated.

## Quickstart

```bash
uv sync --extra dev
uv run pytest

uv run nat2 capture hl --roster      # WORM ingest, dual-timestamped — start this first
uv run nat2 gate feed                # is the data intact and causal?
uv run nat2 wallets seed             # leaderboard-seeded registry (equity + volume)
uv run nat2 cycle                    # snapshot, then observe liquidations
uv run nat2 roster show              # which pairs are observed, and why
uv run nat2 map BTC --rank           # liquidation map, coverage, and which cluster ranks
uv run nat2 gate map                 # and whether it earns the right to be built on
uv run nat2 actions --since 24h      # what the system did: ops, observations, research
uv run nat2 log verify               # the ledger is tamper-evident; check it
```

Capture accrues calendar time. Point-in-time series cannot be recovered later, so the daemon runs
while the rest is built.

## Layout

```
src/nat2/
  hl/         ws · info · leaderboard · ratelimit · schemas   ← only package that knows HL exists
  io/         worm · capture · compact · snapshot · liqscan · mapsnap · replay · cycle · tape · actions
  core/       clock · registry · roster · schedule · guard · costs · paths · reconstruct · errors
  features/   liqmap · liqmath · liquidations · fills · bars · context · frame · spec · attack
  labels/     barriers
  experts/    base · magnet_a · magnet_alpha
  validate/   audit_feed · wfo · calibrate · evaluate · placebo
  gates/      feed · map · magnet
  ledger/     chain
deploy/       gapwatch · statuspage · report · gates_run · evlog/ · systemd_units
```

Layers only call downward — that is what lets the simulator and the live path share code honestly.
Storage is append-only `ndjson.zst` (WORM) → Parquet; SQLite holds mutable state only. A closed
file is checksummed into a manifest and never reopened, so a changed digest always means
corruption and never merely a bounced daemon. `deploy/` is stdlib-only where it must survive the
repo's own venv being broken (`gapwatch`, `statuspage`, `gates_run`).

Python 3.12 + uv · polars · typer · rich · websockets/httpx · pydantic.

## Status — 2026-08-22

| gate | state | why |
|---|---|---|
| `feed` | FAIL | capture holes on 08-20…22 (below); re-runs clean after a gap-free day |
| `map` | refused | `insufficient_forward_events` — the forward window opened 08-20 14:31 (ledger seq 119) |
| `magnet` | refused | `upstream_map`; it also needs 2000 forward scoreable events ∧ 30 distinct days |

Capture has run under [`packaging/systemd`](packaging/systemd/) since 2026-08-13 — **with a hole
from 08-14 to 08-19**, so the clock the gates count from is 08-20, not the 08-13 in
`HYPOTHESIS_1.md` §8. Earliest honest verdicts: **`gate map` ~08-27/29**, **`gate magnet`
~09-18/19**. Dates are recomputed from `accrual()` on every gate run and shown on the status
page; they are never copied from a document.

Also true, and recorded in [`FINDINGS.md`](FINDINGS.md) rather than smoothed over: an idle WORM
flush kept the tape's mtime fresh through every websocket silence, so **243 minutes of holes in
2.5 days were booked as 14** before it was found; and the map's band imbalance **changes sign
between the 5 % and 10 % bands** ($573M of BTC mass sits beyond ±5 %, against $112.6M inside it),
so "which way does the magnet pull" has no band-independent answer.

## What is being built, and what it is waiting on

The programme is a reduced scope of numbered tasks; the task files themselves live outside this
repo, and this is what they add up to. **Built** means merged here with its tests and its
validation actually executed.

**The observatory** — continuous multi-pair observation that reports what it did and ends in
models, rather than in a notebook.

| | intent | state |
|---|---|---|
| Hetzner primary | capture, cycle, watchdogs, nightly restic backups on a VM; the dev box demoted to a second, independent tape | software **built** — host profiles in `systemd_units.py`, boot-hole measurement in `gapwatch`, `nat2 tape compare` for two tapes hour by hour; **the deploy itself waits on** a VM, DNS, an ntfy topic, a Caddy password hash and Storage Box credentials |
| pair roster | `pairs.toml` declares the observed set (top-N by volume + pins), the map universe (coverage ≥ 25 %), and a B-roster of builder-deployed perps that is observed but never promoted; every change is a ledger entry | **built**; the B-roster is empty until capture polls per-dex cross-sections, and its admission rule is written but deliberately unregistered |
| action log | `data/actions.jsonl` at four levels that are never blurred — L0 ops, L1 observation, L2 research, **L3 signal, which stays empty until a gate PASSes** | **built** |
| daily digest | one static HTML page per day (weekly on Mondays): system health, gate ladder with accrual bars, the per-pair table, scheduled events ahead, the action log, incidents; one ntfy line | **built**; timers `nat2-gates` 06:30 and `nat2-report` 07:00 render but are not yet installed |
| model artifact | a model exists only as the output of a gate PASS: parameters, calibration, cost hash and data window frozen to `models/`, hashed into the ledger, with a model card that states what would refute it and when it is reviewed; a **refuted** model is frozen the same way | planned — buildable on synthetic ledgers now, exercised for real at the first verdict |
| shadow book | from a PASS onward, what the frozen model *would* have done: simulated fills at the next print, net of a hash-stamped cost model. Never an order | planned, gated on a PASS |

**The research questions**, in the order the evidence allows.

| | intent | state |
|---|---|---|
| labelling cost | one cell with 200 placebo replications has to finish in minutes, or no verdict finishes at all | **built** — 8.4 min per cell, labels provably identical, 8 GB → 0.56 GB |
| α-kernel expert | HYPOTHESIS_1 §6 criterion 2 — "some α > 0 beats α = 0" — needs a kernel exponent the learned expert does not expose | **built** under its own pre-registration (ledger seq 153); `gate magnet` can now PASS |
| calibration | isotonic is fitted on the rows it scores, which puts a null-world model *below* the constant floor; cross-fitting is the fix | pre-registration written, **waiting to be registered** — it must land before the first verdict, not after |
| wider map | at a one-week horizon σ√T ≈ 8 %, so the whole ±5 % of the shipped map sits inside one barrier width. `nat2.liqmap2` persists ±30 % with 0.25 % buckets — 2.2× the disk, because empty buckets are not stored | **built**; accruing since 2026-08-22. A long-horizon hypothesis is written against it no earlier than 2026-09-21, and nothing reads it before that |
| accelerator | the magnet claim has a second half: once price *touches* a cluster, does the sweep extend or snap back? The label exists and is used by nothing | next — a touch census first, so the runnable-when is measured rather than guessed, then a pre-registration for review |
| attack ratio | `features/attack.py` already answers "is this cluster big enough and close enough to be worth pushing into", with exponents forced by the impact law rather than fitted, and is wired to nothing | planned — descriptive columns first; as an expert only under its own entry, after the magnet verdict |

If `gate magnet` FAILs, that is a result and the next primary question is already named: whether
wallets position correctly *before* information events, which is why the event timeline has been
capturing since 08-20.

## Documents

- [`ASKING.md`](ASKING.md) — how to read the liquidation map: what to ask, which flags are not
  cosmetic, and the five ways to get a plausible wrong answer.
- [`docs/DESIGN.md`](docs/DESIGN.md) — the full design: capture, features, experts, meta,
  allocation, execution, milestones, and the risks worth naming.
- [`TASKS.md`](TASKS.md) — the original next-steps list and the open questions it opened.
- [`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) — the magnet claim, pre-registered: null, controls and
  decision rule fixed before the data. The concrete form of `gate magnet`.
- [`CONSISTENCY.md`](CONSISTENCY.md) — how the drift field is estimated, and the cross-cell
  constraint that supplies the power the sample size does not.
- [`ATTACK.md`](ATTACK.md) — the function itself: why a cluster is worth pushing into, and why
  its exponents are forced by the impact law rather than fitted.
- [`FINDINGS.md`](FINDINGS.md) — what running the system actually taught, including the defects
  it found in itself.
