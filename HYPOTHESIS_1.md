# H1 — liquidity as a magnet

**Pre-registration. Written before the data exists, which is the only time it counts.**

The claim under test is that a liquidation cluster changes where price goes next: that mass
asymmetry in the map shifts the probability of which side price reaches first, and that the shift
is larger for mass that is bigger and nearer. This document fixes the design, the null, the
controls and the decision rule now, so that the result cannot be chosen after seeing it.

It is the concrete form of `gate magnet`, and it is `nat2`'s first falsifiable claim about the
market rather than about the data.

Status: pre-registered, not started. **Nothing accrues until capture runs.**

---

## 1. The claim, stated so it can die

> For a coin at mark `M`, let the map hold liquidation notional at prices above and below.
> Price is more likely to reach the side carrying more mass, and mass closer to the mark counts
> for more than mass further away.

The falsifiable version, and the one actually tested:

> **H1.** In a symmetric two-barrier race, distance-weighted mass asymmetry predicts which
> barrier is touched first, beyond what unweighted band imbalance already predicts, and beyond
> what the geometry of the price history alone would produce.

Three things have to be true together. Any one of them failing kills H1:

| | must hold | fails if |
|---|---|---|
| **signal** | mass asymmetry shifts first-passage probability off 0.5 | the shift is zero, or the wrong sign |
| **distance** | a distance kernel beats no kernel | `α = 0` wins model selection |
| **substance** | the shift survives a size-preserving permutation of the map | the effect is geometry, not mass |

The third is the one that matters most and is the one most likely to be skipped.

---

## 2. This is not a gravity model, and calling it one causes errors

The idea arrives dressed as gravity — a force falling off with distance — and the metaphor is
wrong in a way that changes the specification.

Gravity is a **continuous field**. A liquidation is a **threshold**. A position at −5% exerts
exactly zero mechanical influence on price until price arrives at −5%, at which point it exerts a
large one. There is no force at a distance here; the mechanism is a step function at `d = 0`.

Two consequences carry through the whole design:

**Any pre-touch pull is reflexive, not mechanical.** It exists only because other participants
anticipate the touch and position for it. That is a real, tradeable phenomenon, but it is a
*belief* effect, so it should be stronger for clusters that are visible and it may decay as the
trade crowds. Both are testable and both are tested here (§7). A mechanical effect would do
neither.

**The distance kernel is a nuisance parameter, not a discovery.** `α` is not a physical constant
waiting to be measured; it is a shape for aggregating mass, and fitting it continuously against
the number of independent observations available is how a flexible family fits noise. So `α` is
**selected from three pre-registered candidates, never fitted** (§4).

The honest physical analogy is a barrier option or a fuse: nothing, nothing, nothing, then
everything. The observed record already says so — 93% of the liquidated notional in the first
197-event scan arrived inside two minutes.

---

## 3. The null is free, and the code already produces it

`labels/barriers.py:race()` places the opposite barrier symmetrically about `p0`. Under a
driftless random walk with symmetric absorbing barriers, the probability of touching either side
first is exactly **0.5**. So the null needs no model, no fitting and no simulation, and the entire
hypothesis reduces to one question: **does mass asymmetry move that number?**

`features/liqmap.py:imbalance(band)` is the unweighted mass asymmetry — the `α = 0` case,
band-limited. It is therefore both the **mandatory baseline** (`Expert.baseline()`, enforced by
`nat2 eval`) and a *nested special case* of the model. That nesting is deliberate: model
selection between `α = 0` and `α > 0` is then a clean comparison rather than a contest between
different objects.

**The barriers must be placed independently of the map.** Placing the target barrier *at* the
cluster makes the barrier location a function of the map, which leaks distance-to-cluster into
the null and manufactures the result. Barriers are at `±k·σ`, chosen without reference to the
map, and the map is only ever a covariate.

---

## 4. Specification, fixed now

**Distance** is measured in horizon-scaled volatility units, never percent. A cluster 2% away is
adjacent at 4% daily vol and unreachable at 0.5%, and a raw-percent kernel across a mixed
universe is misspecified before the first observation.

```
d_b = |p_b − M| / (σ · √T)          floored at d_min = 0.25
```

The floor is not cosmetic: at `α > 0` the kernel is singular as `d → 0`, and mass sitting on top
of the mark would otherwise dominate every reading. `d_min` is pre-registered and does not move.

**Mass asymmetry**, sign convention matching `imbalance()` — positive means more mass below,
i.e. a predicted pull downward:

```
              Σ_below m_b · d_b^−α  −  Σ_above m_b · d_b^−α
    A_α  =   ─────────────────────────────────────────────
              Σ_below m_b · d_b^−α  +  Σ_above m_b · d_b^−α
```

**Mass is not notional.** What acts on price is the impact of the forced flow, which is notional
against available depth. `m_b` is notional scaled by the liquidity block (`book_thin`, `amihud`),
not raw dollars. $50M liquidating into a deep book is a smaller event than $5M into a thin one.

**Cross and isolated are different objects and enter separately.** An isolated position is a true
point mass at a fixed, known price. A cross position's liquidation price *moves* whenever any
other position in that account moves, so cross mass is a distribution over prices that drifts
toward the mark as the account takes losses elsewhere. `LiqMap` already separates `up_cross` /
`down_cross`; H1 uses them as **separate terms with their own kernels**, not as a diagnostic
column. This is not a refinement — the first BTC map read under this design carried $76.6M at
+3.56% that was **100% cross** against $44.7M at −2.56% that was **0% cross**. Summing those as
comparable masses is a specification error large enough to decide the result on its own.

**The pre-registered grid, and nothing beyond it.** Every additional cell raises the deflated
Sharpe bar by `√(2·ln N)` for free, so the grid is small and closed:

| | values | count |
|---|---|---|
| kernel `α` | 0, 1, 2 | 3 |
| barrier `k` (σ) | 1, 2 | 2 |
| horizon `T` | 1h, 4h, 24h | 3 |

18 cells. **No cell is added after data is seen.** A cell that looks interesting and was not
pre-registered is a new hypothesis and gets a new document.

**Universe:** coins whose map coverage clears the `gate map` floor (25%) at the decision time.
Coverage enters the model as an interaction term, so a low-coverage reading is allowed to be
less informative rather than equally trusted.

**Sampling:** races overlap heavily, so uniqueness weights are mandatory and the reported `n` is
the **non-overlapping** count. Overlapping samples inflated an earlier result from 0.39 to 0.06
in the sibling project; that lesson is imported, not re-learned.

---

## 5. The controls — without these the result is worthless

**The confound that will hand us a false positive.** Liquidation clusters sit where price has
already been: longs are liquidated below where longs entered, and entries cluster at support, at
prior-day ranges, at round numbers, at high-volume nodes. Cluster location is therefore
*downstream of price history*, and a naive test will find "price moves toward clusters" partly
because clusters mark levels where mean reversion already operates. **A positive result is
expected under the null and means nothing on its own.**

| control | what it removes | how |
|---|---|---|
| **permutation placebo** | geometry masquerading as mass | rebuild the map with masses shuffled across the observed cluster *locations*, preserving both the location distribution and the size distribution. Full pipeline re-run. The effect must vanish. |
| price-history covariates | support / resistance | distance to prior-day high and low, distance to session VWAP, trailing realized trend |
| coverage interaction | seeing a fifth of the book | coverage as an interaction, not a filter |
| vault exclusion | flow that is crowded by construction | vault addresses tagged and excluded, as elsewhere in the system |

The permutation placebo is not optional and is not a robustness check to run if time permits. It
is the difference between a claim and an anecdote.

---

## 6. Decision rule, committed before the result

Evaluated per cell on purged walk-forward with embargo `T`, isotonic calibration on OOS folds
only.

**CONFIRMED** requires all four:

1. Out-of-sample accuracy beats the `sign(imbalance)` baseline in **≥ 12 of 18** cells.
2. Some `α > 0` beats `α = 0` on out-of-sample log loss in a **majority** of `(k, T)` cells.
3. The effect **collapses under the permutation placebo** (the placebo's edge is indistinguishable
   from zero at p < 0.01).
4. The implied edge exceeds `2 × (fees + funding + slippage) / σ`, computed by
   `costs` against a hash-stamped `costs.toml`. Funding is **hourly** on HL and is charged.

A probability shift from 50% to 53% is a real finding and pays for nothing. Criterion 4 is what
separates the two, and it is a function call, not a judgement.

**REFUTED — permanent, do not reopen:**

- `α = 0` wins model selection *and* the mass term adds nothing over `sign(imbalance)`. The
  distance kernel is worthless and the magnet is, at most, the band imbalance already shipped.
- The effect survives at full strength under the permutation placebo. It was geometry.
- The edge is real but smaller than cost at every reachable fee rung. Dead by arithmetic, which
  is the cheapest death available and should be checked first.

**UNDECIDABLE — not refuted, and the distinction is the point:**

- Fewer than the minimum non-overlapping observations in a cell (§8).
- Coverage never clears the floor for enough coins.

Filing *undecidable* as *refuted* is the single most expensive error available here. A cell that
never accumulated its sample says nothing about the market; it says something about the clock.

---

## 7. Two tests that only a reflexive effect can pass

If the pull is a belief phenomenon (§2) rather than a mechanical one, it carries fingerprints.
These are pre-registered as **descriptive**, not gating — they do not decide H1, they interpret it.

- **Visibility.** Effect size conditioned on cluster prominence — mass rank, proximity to a round
  number, whether the cluster is large enough to appear on public dashboards. A mechanical effect
  should not care. A reflexive one should.
- **Decay.** Effect size over calendar time. The exact map is exact for hunters too, so a crowding
  trade should weaken. A finding that strengthens monotonically deserves suspicion, not celebration.

---

## 8. Power, and what the clock actually permits

Every bar starts a race, so H1 is enormously better powered than the cascade-fade branch, which
is limited by rare events. But overlapping races are not independent, and the honest count is
`span / T` per coin.

Assuming ~10 coins clear the coverage floor:

| horizon | non-overlapping obs / day | 2,000 obs reached in | first honest read |
|---|---|---|---|
| 1h | ~240 | ~8 days | **~2 weeks after capture starts** |
| 4h | ~60 | ~33 days | ~5 weeks |
| 24h | ~10 | ~200 days | ~7 months |

**Minimum to read a cell: 2,000 non-overlapping observations spanning ≥ 30 distinct days.** The
day-count floor matters independently of the observation count — 2,000 observations from four
volatile days is one regime measured repeatedly, not a sample.

The 1h and 4h cells are the deliverable. The 24h cell accrues quietly and is read when it is
ready, or never; it does not gate anything.

**The clock has not started.** There is no capture daemon and no store. Every date above is
relative to the day `nat2 capture hl --all` begins running continuously, and no later code can
recover a day not captured. That is the only thing on this page that is urgent.

---

## 9. Prerequisites

| | state | blocks |
|---|---|---|
| `gate feed` PASS | done | everything |
| capture running continuously | **not started** | every date in §8 |
| map snapshots persisted (`io/mapsnap`) | built | the as-of map for each decision time |
| coverage ≥ 25% on ≥ 10 coins | partial — BTC 19%, ETH 29.5%, SOL 22.5% on a 800/3601 sweep | universe size |
| feature frame with as-of joins | built | the decision-time state vector |
| two-barrier race labels | built | the label |
| purged WFO + calibration | **not built** | evaluation |
| `costs.toml`, hash-stamped | **not built** | criterion 4 |

H1 is not blocked on ideas. It is blocked on a running daemon and two pieces of validation
machinery.

---

## 10. Verify before coding

Each needs a test that fails loudly if the answer moves:

- **`OI_SIDES = 2`.** A factor of two on coverage, which gates the universe. Unverified against HL
  docs.
- **σ estimator.** Which realized-volatility estimator, over which window, scaled to `T` how. Fix
  it once; a vol estimator changed mid-study invalidates every cell measured before the change.
- **Mark vs oracle for barrier evaluation.** Barriers evaluate on **mark**, because that is what a
  position experiences and what triggers liquidation. Confirm the trigger price is mark and not
  index.
- **Depth normalisation.** `hl.l2book` cadence is sparser than the documented hint (45 records /
  70s across 3 coins). Measure it properly before `m_b` depends on it.
- **Funding cadence and clamp.** Hourly on HL, and charged in criterion 4. Not a rounding error
  at 4h and 24h.

---

## 11. What this unblocks, and what it ends

CONFIRMED promotes `magnet_a` from a design sketch to an expert with a measured edge, gives
`gate magnet` its verdict, and makes the Stage B fade worth building — the fade has the better
mechanism but far worse power, so H1 is the cheap test that earns the expensive one.

REFUTED closes the magnet branch permanently and removes `magnet_a` and `magnet_b` from the
design. That is a good outcome delivered in weeks rather than a suspicion carried for months, and
the refusal is the product.

The one outcome to guard against is neither: an interesting number, produced by geometry,
believed because it was hoped for, and built upon. Section 5 exists for that, and section 6 was
written before the data so that it cannot be edited afterwards.
