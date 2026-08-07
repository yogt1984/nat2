# nat2 — design

A terminal research and execution engine for **Hyperliquid only**. HL is the one venue where the
liquidation map and the trader population are exact data rather than reconstructions; this
system exists to find out whether that exactness is worth anything.

Status: design only.

---

## Two rules

**Gates before models.** The source material is a list of experiments that are allowed to end
the project. So gates are CLI commands, each writing a PASS/FAIL verdict to a hash-chained
ledger, and **every downstream command refuses to run when its upstream gate is missing or
FAIL.** That refusal is the product.

**Exact or nothing.** No estimated maps, no assumed leverage mixes, no aggregator history.
Where a quantity can't be had exactly, don't compute it — the approximation is what makes a
backtest lie.

| Gate | Question | Kills |
|---|---|---|
| `feed` | is the data intact and causal? | everything |
| `map` | does the map predict HL's *own realized* liquidations, at adequate coverage? | the map branch |
| `magnet` | does the cluster pull, net of costs, better than `sign(imb)`? | magnet experts |
| `persistence` | does wallet skill in `[t−90d, t]` predict `(t, t+30d]`? | the smart-money branch |
| `decay` | does cohort-flow alpha survive past minutes? | copyability |

---

## Why HL-only works

HL liquidates off a mark price built from a **CEX-derived oracle that HL publishes**. So
`premium = (mark − oracle)/oracle` is a native, exact measure of the global pressure a Binance
sidecar would have supplied — no second venue, no second clock, no second cost model. And the
causal arrow points the right way: global price drives liquidations against a map we observe
exactly.

Given up, honestly: liquidation density on other venues (only ever an *estimate*, which rule 2
rejects anyway); long history across many regimes (handled with wider embargo and smaller
models, never by borrowing CEX history); the thin-alt cascade tail.

---

## Layers

```
L-1  capture    WORM ingest of HL WS + node data, dual-timestamped     [daemon]
L0   features   pure bar→vector functions with causality contracts
L1   experts    each emits a calibrated probability or score
L2   meta       Hedge / fixed-share over net-of-cost expert PnL
L3   allocate   vol target → capacity cap → HL margin caps → liq-distance floor
L4   execute    simulator | testnet | mainnet (flag-gated, off)
L5   ledger     hash-chained research log + trade journal
```

Layers only call downward. That is what lets the simulator and the live path share code honestly.

---

## Capture

**Dual timestamps.** Every record carries `t_event` (HL's clock) and `t_ingest` (when we could
first have known). A feature for a bar closing at `T` reads only `t_ingest ≤ T`. This makes
lookahead queryable instead of a code-review hope; `nat2 audit causal` enforces it by replay and
FAILs on any diff.

**Reconstruct, don't poll.** Polling `clearinghouseState` per wallet makes HL's rate limits the
map's *resolution*. Instead: rebuild positions from the fill stream, derive liquidation prices
using per-asset `maxLeverage` from `meta`, and use `clearinghouseState` only to reconcile a
rotating sample and alarm on drift. Cross and isolated margin get separate paths — a cross
wallet's whole map moves when any one of its positions moves.

**Start day one.** Point-in-time series can't be recovered later. The daemon runs while the rest
is built; day 1 of M0 is the project's real start date.

Streams: `trades`, `l2Book`, asset contexts (mark/oracle/funding/OI), `userFills`/`userEvents`,
node-data fills for backfill, `meta` for the universe, HLP/vault state. No other venue appears.

Storage: append-only `ndjson.zst` (WORM) → Parquet → DuckDB views; polars for math; SQLite for
mutable state only. A file, once closed, is checksummed into a manifest and never reopened — a
restart inside the same hour starts a new part, so a changed digest always means corruption and
never merely a bounced daemon.

---

## Features

One map, exact: notional per price bucket, split cross/isolated, decayed as positions close.
Every map carries **coverage** = `Σ registry notional / venue OI`, printed on every card and
gate-thresholded. A map without its coverage number is a lie with a chart on it.

```
L_up(b), L_dn(b)   liq notional in band b ∈ {0.5,1,2,5}% ÷ trailing dollar volume
imb, imb_cross     (L_dn − L_up)/(L_dn + L_up); the second on cross margin only
d_near, book_thin  σ-distance to nearest cluster; depth in between ÷ average
premium, premium_z (mark − oracle)/oracle          ← global pressure, HL-native
oi_z, fund_z       funding is HOURLY on HL — not a rounding error at these horizons
liq_flow           realized liquidation notional, from HL prints
hlp_delta          backstop-vault inventory change  ← cascade absorption / exhaustion
sigma_regime, tau  vol state; bars since last cascade
amihud, spread_eff, depth_xbps, vol_stability      general-liquidity block
smart_flow         equity-weighted net signed flow of the skilled cohort
```

`hlp_delta` has no CEX analogue: when forced flow overwhelms the book, HL's backstop vault
absorbs it, so vault inventory change *measures* how much of a cascade went unabsorbed.
Exhaustion becomes observable instead of inferred.

The general-liquidity block does three jobs, kept apart in code: **gates** the universe,
**scales** the cost model, **conditions** the state vector. Never a return signal alone.

**Labels.** Two-barrier race for Stage A ("eventually touches" is a garbage label). Triple
barrier on touch events for Stage B. Uniqueness weights — cascade episodes overlap heavily.
Barriers evaluate on mark price, because that is what the position experiences.

---

## Experts

```python
class Expert(Protocol):
    horizon: timedelta
    universe: UniverseRule
    def fit(self, train) -> Model: ...
    def predict(self, state) -> Series: ...   # calibrated p, or cross-sectional score
    def baseline(self) -> "Expert": ...       # the dumb thing it must beat
```

`baseline()` is mandatory and is the point of the protocol: GBDT-vs-`sign(imb)` and
clone-vs-raw-flow are the same rule, written once and enforced for every expert by `nat2 eval`.

`magnet_a` (Stage A race) · `magnet_b` (Stage B fade, uses `hlp_delta`) · `smartflow` ·
`clone` — `P(skilled trader enters | state)` · slow baselines `tsmom`, `xsmom`, `carry`.
LightGBM depth 3: HL's short history punishes variance, and the value is in features and labels.

Validation is a library, not a habit: purged walk-forward with embargo `h`, isotonic calibration
on OOS folds only, thresholds set so edge > `2 × (fees + funding + slippage) / σ`. The `c/σ`
arithmetic is a function call, not a comment.

**Cohort, HL-specific.** One address's perps, spot and vault activity are all visible, so
intra-HL hedges are detectable — Trap 2 partially closes. Vault addresses are tagged and
**excluded by default**: their flow is crowded by construction and deposits move positions for
reasons unrelated to any view. Reflexivity is sharper here, not softer — the exact map is exact
for hunters too, so the cohort is skilled-but-*unfamous*, and our own liquidation distance is
monitored by the same code that builds the map.

---

## Meta, allocation, execution

Hedge with fixed-share over net-of-cost expert returns: `w' ∝ w·exp(η·r)`, then mixed with
uniform at rate `α`. Fixed-share tracks the *best sequence* of experts — the property you want
on the day an edge stops working. Online, replayable; a weight collapsing to the floor is the
system telling you an expert died.

Sizing chain, each step logged: vol target → per-coin cap → capacity `Q ≤ q·V` from `√(Q/V)` →
HL `maxLeverage` cap → liquidation-distance floor in ATR.

Three backends behind one interface: **simulator** (next-bar-open fills by construction; costs
from a hash-stamped `costs.toml` — a backtest whose cost hash isn't in the ledger is not a
result), **testnet** (what "paper" means here: same API, same signing path), **mainnet**
(flag-gated, off).

HL specifics the execution layer owns: post-only maker-first with taker fallback only when `c/σ`
still clears; native TWAP for size; centralized `szDecimals`/sig-fig rounding (a rejected order
is a bug, not a retry); EIP-712 signing with monotonic nonces; **an agent wallet that cannot
withdraw**; one budgeted rate-limit client shared by capture and execution; native TP/SL triggers
so protection survives process death.

---

## CLI

```
nat2 capture hl [--all]            nat2 backfill --from …      nat2 compact
nat2 registry sync | reconcile     nat2 audit feed | causal
nat2 map show BTC [--live]         nat2 wallets scan | rank | persist
nat2 gate feed | map | magnet | persistence | decay
nat2 spec freeze | verify | list   nat2 features build   nat2 label build
nat2 train expert X                nat2 eval expert X    nat2 meta run
nat2 bt run --spec S               nat2 testnet start | status | reconcile
nat2 mon                           nat2 log add | query | verify
```

`nat2 mon` is a Textual TUI: liq-map column with coverage, state row, position and liquidation
distance, expert weights, and a permanent gate line — a failed gate shows its experts as
DISABLED rather than letting them quietly contribute.

The existing skills (`/liqmap`, `/whales`, `/backtest`, `/research_log`, …) become thin
front-ends that shell out to `nat2`. Guard logic lives in one place or it drifts.

---

## Layout

```
src/nat2/
  cli.py
  hl/       ws info exchange signing ratelimit nodedata schemas   ← only package that knows HL exists
  io/       worm compact views
  core/     clock registry costs universe
  features/ liqmap liquidity flow premium hlp state smartflow
  labels/   barriers weights
  experts/  base magnet_a magnet_b smartflow clone tsmom xsmom carry
  meta/     hedge
  alloc/    sizer capacity
  execute/  simulator testnet mainnet
  gates/    feed map magnet persistence decay
  validate/ wfo calibrate audit_causal
  ledger/   chain journal
  tui/      app panes
```

Python 3.12 + uv · polars · duckdb · lightgbm · typer · textual · websockets/httpx · pydantic ·
eth-account. Rust stays out until there's a latency-bound live loop; `execute/` is narrow so that
swap needs no research changes.

---

## Milestones — each ends at a gate

| # | Deliverable | Ends at |
|---|---|---|
| M0 | `hl/` client, capture daemon, WORM, dual timestamps | `gate feed` — **start immediately** |
| M1 | position reconstruction, exact map, coverage | `gate map` |
| M2 | labels, `magnet_a`/`magnet_b`, purged WFO, calibration | `gate magnet` |
| M3 | node-data backfill, per-wallet equity, vault tagging | `gate persistence` — **go/no-go for smart money** |
| M4 | cohort `smart_flow`, alpha-decay study | `gate decay` |
| M5 | meta-learner, sizer, simulator, testnet | first end-to-end run |
| M6 | TUI | |

M3 and M4 are ~a week each and are each allowed to end their branch. That is why they precede M5.

---

## Risks worth naming

Reconstruction drifting from truth (continuous sampled reconcile; drift FAILs `gate feed`) ·
cross/isolated modelled wrong (both diffed against HL's own `liquidationPx`) · overfitting on
overlapping cascades (purge, embargo, uniqueness weights, mandatory baseline) · crowding, since
the exact map is exact for hunters too (edge is the conditional odds *after* the touch, and
fixed-share demotes decayed experts) · oracle-manipulation tail, JELLY-style (position caps,
OI-share cap, no illiquid tail) · short history (wider embargo, smaller models — never pad with
CEX data) · single-venue operational risk (accepted explicitly) · universe churn (rebuild from
`meta` each run; builder-deployed perps excluded by default).

---

## Verify before coding

Nothing here is hardcoded; each must be read from the API or confirmed against current HL docs,
with a test that fails loudly if it moves: liquidation formula and maintenance margin vs
`maxLeverage` (diffed against `clearinghouseState.liquidationPx`) · mark-price construction and
which price triggers liquidation · funding formula, cadence, clamp · HLP/ADL thresholds ·
rate-limit weights · node-data format, retention and cost (this sizes M3) · fee tiers and
rebates · rounding rules and order-type semantics · usable mainnet history depth (this sets
embargo width).

## Open questions

1. **Registry scope** — whole-venue reconstruction from node data (exact, heavy) vs a top-N
   registry (cheap, caps coverage). Decides whether M3 is one week or three.
2. **Bar clock** — time bars for v1; revisit dollar bars after `gate magnet`.
3. **Universe breadth** — majors and liquid mid-caps, or reach into the alt tail where cascades
   are strongest and data quality is worst? Let the liquidity block decide at M2, not taste.
