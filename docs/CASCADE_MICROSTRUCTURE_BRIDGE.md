# CASCADE_MICROSTRUCTURE_BRIDGE

**Shared document.** Lives at `nat/docs/specs/CASCADE_MICROSTRUCTURE_BRIDGE.md` and
`nat2/docs/CASCADE_MICROSTRUCTURE_BRIDGE.md`. It describes work that spans both repos and
belongs to neither; edit one, copy the other, and say so in the commit.

**Date:** 2026-09-02 · **Companions:** `nat2/HYPOTHESIS_2.md` (the claim), `nat2/CONSISTENCY.md`
(the estimator), `nat/FEATURES.md` (the 239-vector), `nat/docs/METHODOLOGY.md` (the loop)

---

## What this is, in one sentence

> A read-only bridge that joins `nat`'s 239-feature order-book vector onto `nat2`'s liquidation-map
> frame under `nat2`'s existing as-of and refusal rules, and uses it to measure — descriptively,
> with a matched control group and no model — how imbalance and entropy behave around cluster
> touches, so that `gate accelerator` can be answered with microstructure rather than without it.

## What this is not

Stated first because the failure mode is drift toward these:

- **Not a cascade predictor.** No classifier, no net, no P(cascade) estimate. See §2.
- **Not a strategy.** Nothing here sizes, signals, or sends. `nat2`'s no-order rule holds unchanged.
- **Not a new gate.** `gate accelerator` exists and is pre-registered at ledger seq 191. This
  supplies it with better inputs; it does not restate or relax its criteria.
- **Not a merge of the two repos.** The bridge is one-directional and read-only: `nat2` reads
  `nat`'s feature parquet. `nat` gains no dependency on `nat2` and is not modified.

---

## 1. Standing on what exists — do not rebuild

Both repos already contain most of the machinery. This plan is scoped to the gap, and any task
that reimplements a row below is out of scope by definition.

| Already built | Where | What it gives |
|---|---|---|
| As-of frame with `t_decision` | `nat2/features/frame.py` (267 ln) | the look-ahead discipline; `MAP_COLUMNS`, `coverage`, `map_age_s`, null-never-zero |
| Touch detection | `nat2/labels/touch.py` (125 ln) | the episodes: map-predates-print, $50k cluster floor, one touch/shell/hour |
| Barrier labels | `nat2/labels/barriers.py` (226 ln) | the outcome side |
| Accelerator gate | `nat2/gates/accelerator.py` (202 ln) | H2's decision rule, mass-blind control, permutation placebo, refusal semantics |
| Magnet gate + expert | `nat2/gates/magnet.py`, `nat2/experts/` | the `sign(imb)` baseline and kernel comparison |
| Hash-chained ledger | `nat2/ledger/chain.py` | pre-registration before scoring code |
| 239 features @100 ms | `nat/rust/ing-features/`, `nat/data/features/` | 8 `imbalance_*`, 27 `ent_*`, 100 days of history |
| Feature manifest | `nat/FEATURES.md` | declared lookbacks, warmup dependence |

**The gap is exactly one thing:** `nat2`'s frame has map columns and its own bars, and no
microstructure. The question "how do imbalance and entropy behave in a cluster" cannot be asked
from either repo alone.

---

## 2. The binding constraint, measured

```
nat/data/features/          100 days   2026-04-19 → 2026-09-02   (31 G)
nat2 liqmap snapshots        19 days   2026-08-09 → 2026-09-02
intersection                 19 days   with a 6-day hole, 2026-08-14 → 2026-08-19
```

`nat` has five months of features; the map has three weeks, so the joint sample is three weeks and
grows one day per day. On BTC/ETH/SOL a resolved cluster touch of the size worth caring about is a
few-per-month event, so the realistic episode count today is **O(10–30), not thousands**.

Three consequences, and they are the reason §"What this is not" reads as it does:

1. **No learned model is admissible at this n.** A model with more parameters than episodes fits
   them exactly and generalises to nothing, and the FDR control in both repos will correctly refuse
   whatever it produces. Deferring the model is not caution, it is the only available option.
2. **The unit of analysis is the episode, not the bar.** 19 days at 100 ms is ~16 M rows, and every
   statistic computed per-row will overstate its own significance by three orders of magnitude.
   Phase 2 emits episodes for this reason.
3. **Capture is the long pole and it is already running.** Point-in-time series cannot be recovered
   later. The daemon staying up is worth more to this hypothesis than any code in this document.
   The 6-day August hole is permanent; a second one is avoidable.

**Coverage.** `FINDINGS.md`: BTC ~30%, ETH ~36%, SOL ~27% of true position notional, after the 2×
OI denominator correction. The unseen mass is not missing at random — wallets below the leaderboard
cut are smaller and more leveraged, so they sit nearer their liquidation price and go first. The
map is therefore structurally short the trigger population, which is a *bias*, not noise. This is
survivable for a conditional description and fatal for a probability estimate, which is the second
reason §"What this is not" forbids one.

---

## 3. The selection trap

`HYPOTHESIS_2.md` §1 states it and it governs every phase here:

> a touch is selected on price *having moved*, so the sample is conditioned on a move, and "price
> kept going after a big move" is what momentum alone predicts.

The intuition that draws one to cascades — *the moves are larger* — is that trap phrased as an
attraction. Largeness is what selected the episode. Any separation found between touch episodes and
the unconditional population is therefore uninterpretable.

**So the comparison is never touch-vs-all-bars.** It is touch-vs-matched-control, where a control
is an approach into the same distance band and mass band that did *not* resolve into a touch. This
is the `nat3` construction — record the forward paths of the *rejected* candidates so the filter
becomes falsifiable — applied to clusters. Without the control group this study is unfalsifiable in
the precise sense `nat3/RESEARCH.md` describes, and should not be run.

---

## 4. Phases

Each phase has an artefact and an exit condition. A phase that cannot meet its exit condition stops
the plan; that stop is a result and is recorded, not worked around.

### Phase 0 — Pre-registration *(before any scoring code)*

Append to the ledger, per `nat2`'s standing rule: the feature subset (§5.1), the distance and mass
bands, the matching tolerance, the episode window, the control ratio, and the summary statistics
Phase 3 will compute. Fixed before the joined data is looked at.

**Exit:** ledger entry written, `nat2 log verify` intact.
**Refusal:** everything downstream refuses without this entry, same mechanism as the gates.

### Phase 1 — `bridge()`: the cross-repo join

A read-only reader in `nat2` for `nat`'s feature parquet, and a widening of the existing frame.
Contract in §5. No new frame — extend `features/frame.py`'s as-of machinery, which already
implements the hard part.

**Exit:** `nat2 frame BTC --with-micro` emits rows carrying both column families; the null-rate per
column is reported; a planted-test proves a late-arriving feature file cannot enter an earlier bar.

### Phase 2 — Episodes and controls

Wrap `labels/touch.py` output into episodes — approach → touch → aftermath — and generate the
matched control set: approaches into the same (distance, mass, coverage) cell that did not touch,
at a pre-registered ratio. Both carry the full joined vector.

**Exit:** an episode table with `n_touch` and `n_control` stated, per-coin and per-day, plus the
distinct-day count. **If `n_touch` is below `gate accelerator`'s resolved-touch and distinct-day
floors, stop here and report the shortfall** — that is the honest outcome of a 19-day overlap and
requires no further code.

### Phase 3 — The descriptive study

For each pre-registered feature: the conditional distribution across the episode window, touch
versus control, with confidence intervals that use the episode count as n. Cross-sectional
consistency across BTC/ETH/SOL as the only out-of-sample structure available at this sample size.

**Exit:** a findings section stating, per feature, whether touch and control separate — including,
explicitly, the finding that none do.

### Phase 4 — Feed `gate accelerator`, or stop

If and only if Phase 3 shows separation, the microstructure columns become admissible inputs to the
existing accelerator expert, and the gate runs under its own unchanged criteria — including the
mass-blind control, which is what distinguishes the map's contribution from momentum.

**Exit:** a gate verdict in the ledger. A refusal is a valid exit.

### Not in scope, and gated on Phase 3

Any learned model. Revisit only when the joint capture reaches a few hundred resolved touches —
on current cadence, **not before mid-2027** — and then with the cost model of §6, not before.

---

## 5. The `bridge()` contract

Six properties. Five are about refusing to lie.

**5.1 Feature subset, declared and small.** Not all 239. The pre-registered subset is the ones the
hypothesis names: the 8 `imbalance_*` and a fixed selection of the 27 `ent_*` (permutation entropy
at m=3/m=5, `ent_permutation_imbalance_16`, `ent_spread_dispersion`). Pulling 239 columns into a
20-episode study is a multiple-comparison machine. Every included column is listed in the Phase 0
ledger entry and declared in `features/spec.py` with its lookback, which is what the walk-forward
embargo is computed from.

**5.2 Point-in-time on both timestamps.** A `nat` feature row may enter a `nat2` bar only when
`t_event ≤ t_decision` **and** the file's arrival time `≤ t_decision`. `nat2`'s bars already carry
`available_at`; `nat`'s parquet is written per-day, so the second condition must come from an
explicit arrival record, not from the filename. Dropping it lets a late-written day leak into bars
that predate its existence — invisible in backtest, fatal in it.

**5.3 As-of backward join with a staleness bound; never forward-fill.** `frame.py`'s rule verbatim,
extended to the new columns: newest observation that arrived strictly earlier, and no older than a
declared `MAX_MICRO_AGE_NS`. Beyond the bound the columns are `None` with a reason — never zero,
never carried. A zero imbalance and a missing imbalance are different facts, and during a cascade
the feed degrades exactly when it matters most.

**5.4 Coverage and staleness travel as columns.** `coverage`, `published_frac` and `map_age_s`
already do; `micro_age_ns` joins them. Pooling episodes at 22% and 36% coverage silently changes
the definition of the independent variable mid-sample, so coverage is a stratification key in
Phase 3, not metadata in a header.

**5.5 Gate status is first-class, and `bridge()` raises.** If `gate feed` or `gate map` is
missing/stale/FAIL over the requested window, `bridge()` raises `GateRefusal` — not a warning, not
a nullable flag the caller may ignore. `nat2`'s refusal semantics are the single property that
makes it worth building on; a layer that averages over them destroys the only thing the ledger buys.

**5.6 Universe stated in the signature.** `nat` computes features for BTC/ETH/SOL only. `bridge()`
accepts those three and rejects the rest by name rather than returning nulls a caller discovers
later.

---

## 6. If Phase 4 ever passes: the cost model

Recorded now so it cannot be chosen later.

`nat`'s Q4 kill gate (2026-07-30) killed 5/5 shipped winners partly on a wrong-venue cost tier. The
same trap is sharper here: a cascade is precisely when the book is thinnest and slippage worst, so
a constant round-trip assumption manufactures an edge that evaporates on contact. Any economic
claim built on this study must use a **regime-conditional cost estimated from observed book state
within the episodes themselves**, and must state the episode count that estimate rests on.

The one thing arguing for the whole exercise is that cascades are the only regime where the taker
path can arithmetically clear cost — `nat`'s 1–5 s move is 0.5–2 bps against ~11 bps round-trip,
while a cascade move is one to two orders larger. That asymmetry is the reason to look. It is not
evidence that anything is there.

---

## 7. Kill criteria

The plan stops, and the stop is written to `FINDINGS.md`, when any holds:

1. Phase 2 yields fewer resolved touches than `gate accelerator`'s floors — *report and wait for
   calendar time*, do not lower the floor.
2. Phase 3 shows no separation between touch and matched control on any pre-registered feature.
3. Separation exists but vanishes under the mass-blind control — the map contributed nothing and
   the result is momentum.
4. Coverage for a coin falls below the level at which its map is interpretable; that coin exits.
5. The joined null-rate exceeds a pre-registered threshold — the two feeds do not overlap densely
   enough to join, independent of what the market did.

---

## 8. Verification

Per `nat/CONVENTIONS.md`, every behaviour change names its check.

| Change | Verification |
|---|---|
| `bridge()` reader | `pytest` unit + planted test: a late-arriving feature file must not enter an earlier bar |
| Frame widening | `nat2 frame BTC --with-micro` on a real overlap day; null-rate per column reported |
| Episode/control builder | planted test: a control matched into the wrong (distance, mass) cell must fail |
| Any gate interaction | `nat2 gate accelerator` end-to-end; `nat2 log verify` after |
| Every phase | ledger entry precedes the scoring code that reads it |

Branch per `CONVENTIONS.md`: `feat/cascade-bridge` off master in each repo, `merge --no-ff`.

---

## 9. Honest summary

The hypothesis is already written (`HYPOTHESIS_2.md`), the gate already exists
(`gates/accelerator.py`), and the missing piece is three weeks of overlapping data — not a method,
not a model, and not an abstraction layer. This plan builds the one bridge that lets the existing
machinery see microstructure, measures the conditional behaviour honestly against a control group,
and then most likely reports that the sample is too small to conclude anything. That report is the
expected deliverable. The daemon staying up is the actual work.
