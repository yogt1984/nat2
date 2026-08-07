# Build prompt

The design in one prompt — self-contained, paste into a fresh agent.

---

Build `nat2`, a terminal research and execution engine for **Hyperliquid only**. Python 3.12 +
uv, polars, duckdb, lightgbm, typer, textual, pydantic, eth-account. No other exchange appears
anywhere in the codebase.

**Thesis to test, not assume:** clustered liquidations act as a magnet — price is drawn toward
dense forced-flow zones, then overshoots and snaps back. HL is the only venue where that map is
exact rather than estimated, and where the trader population is public.

**Two rules that govern every design decision.**

1. *Gates before models.* Five gates — `feed` (data intact and causal), `map` (does the
   reconstructed map predict HL's own realized liquidations, at adequate coverage), `magnet`
   (does the cluster pull net of costs, beating `sign(imb)`), `persistence` (does wallet skill in
   `[t−90d,t]` predict `(t,t+30d]`), `decay` (does cohort-flow alpha survive past minutes). Each
   is a CLI command writing a PASS/FAIL verdict to a hash-chained ledger. **Every downstream
   command refuses to run when its upstream gate is missing or FAIL.** The refusal is the product.
2. *Exact or nothing.* No estimated liquidation maps, no assumed leverage mixes, no aggregator
   history. If a quantity can't be had exactly, don't compute it.

**Why one venue suffices:** HL liquidates off a mark price built from a CEX-derived oracle that
HL publishes, so `premium = (mark − oracle)/oracle` is a native exact measure of global pressure.
No sidecar. Accept the losses openly: other venues' liquidation density, long history, the thin
alt tail.

**Layers, calling only downward:** capture (WORM ingest) → features (pure, causality-contracted)
→ experts (calibrated probabilities) → meta (Hedge/fixed-share) → allocate → execute
(simulator | testnet | flag-gated mainnet) → ledger.

**Capture.** Every record carries `t_event` (HL clock) and `t_ingest` (when we could first have
known); a feature for a bar closing at `T` reads only `t_ingest ≤ T`, and `audit causal` enforces
it by replay. **Reconstruct positions from the fill stream — never poll per wallet**, or HL's
rate limits silently become the map's resolution; `clearinghouseState` reconciles a rotating
sample and alarms on drift. Cross and isolated margin get separate paths. Append-only
`ndjson.zst` → Parquet → DuckDB. **The daemon must start on day one** — point-in-time series
cannot be recovered later.

**Features.** One exact map: notional per price bucket, cross/isolated split, decayed as
positions close, and **every map carries coverage = Σ registry notional / venue OI**, printed and
gate-thresholded. Feature vector, all scale-free: banded liq notional up/down over trailing
volume, `imb` and `imb_cross`, σ-distance to nearest cluster, book thinness between price and
cluster, `premium`/`premium_z`, OI and funding z-scores (**HL funding is hourly** — material at
these horizons), realized `liq_flow`, `hlp_delta` (backstop-vault inventory change — a direct
public measurement of unabsorbed cascade, no CEX analogue), vol regime, bars since last cascade,
a general-liquidity block (Amihud, effective spread, depth, volume stability), and `smart_flow`.
The liquidity block gates the universe, scales the cost model, and conditions the state — never a
return signal alone.

**Labels.** Two-barrier race for Stage A ("eventually touches" is a garbage label). Triple
barrier on touch events for Stage B. Uniqueness weights, because cascade episodes overlap
heavily. Evaluate barriers on mark price.

**Experts** implement `fit`/`predict`/**`baseline()`** — the mandatory baseline is the point of
the protocol, and `nat2 eval` reports the OOS delta net of costs for every expert. Build
`magnet_a`, `magnet_b` (fade, uses `hlp_delta`), `smartflow`, `clone`, plus slow `tsmom`,
`xsmom`, `carry`. LightGBM depth 3 — HL's history is short and punishes variance. Validation is a
library: purged walk-forward with embargo `h`, isotonic calibration on OOS folds only, thresholds
set so edge > `2×(fees + funding + slippage)/σ`.

**Cohort.** Tag and **exclude vault addresses by default** — their flow is crowded by
construction. Target skilled-but-unfamous wallets from your own persistence screen; the exact map
is exact for hunters too, so monitor our own liquidation distance with the same code that builds
the map.

**Meta/alloc/exec.** Hedge with fixed-share over net-of-cost returns (tracks the best *sequence*
of experts — what you want the day an edge dies). Sizing: vol target → per-coin cap → capacity
`Q ≤ q·V` → HL `maxLeverage` → liquidation-distance floor. Simulator fills at next-bar-open by
construction, costs from a hash-stamped `costs.toml`. "Paper" means HL testnet. Execution owns
post-only maker-first, native TWAP, centralized `szDecimals` rounding, EIP-712 with monotonic
nonces, **an agent wallet that cannot withdraw**, one shared rate-limit budget, native TP/SL
triggers.

**Structure.** `hl/` is the only package that knows Hyperliquid exists as a network — one WS
client, one info client, one signer, one rate-limit budget, one schema set. Everything above
works on typed records.

**Build in this order, each milestone ending at its gate:** M0 `hl/` client + capture + WORM +
dual timestamps → `gate feed`. M1 position reconstruction + exact map + coverage → `gate map`.
M2 labels + magnet experts + purged WFO + calibration → `gate magnet`. M3 node-data backfill +
per-wallet equity + vault tagging → `gate persistence` (**go/no-go for the entire smart-money
branch**). M4 cohort flow + alpha-decay study → `gate decay`. M5 meta-learner + sizer +
simulator + testnet. M6 TUI.

**Hardcode none of this — read it from the API or current docs, with a test that fails loudly if
it moves:** liquidation formula and maintenance margin vs `maxLeverage` (diff your derivation
against `clearinghouseState.liquidationPx`), mark-price construction and which price triggers
liquidation, funding formula/cadence/clamp, HLP and ADL thresholds, rate-limit weights,
node-data format and cost, fee tiers, rounding and order-type semantics, usable history depth.

**Stop and ask** before: choosing whole-venue vs top-N registry scope (this decides whether M3 is
one week or three), switching from time bars to dollar bars, widening the universe into the alt
tail, or enabling mainnet.
