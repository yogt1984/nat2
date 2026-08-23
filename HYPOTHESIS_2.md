# HYPOTHESIS_2 — the accelerator: what happens *after* price reaches the cluster

Second of the magnet claim, and the half with the mechanism.

[`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) §2 is explicit that a liquidation is a **threshold, not a
field**: a position at −5% exerts exactly zero mechanical influence until price arrives at −5%,
at which point it exerts a large one. Everything H1 tests is therefore *reflexive* — a pull that
exists only because other participants anticipate the touch. H2 tests the other side of that
sentence, the part that is not a belief: **once price has arrived, does the forced flow carry it
further?**

That makes H2 the more physical claim and the more dangerous one to measure, for one reason. A
touch is selected on price *having moved*, so the sample is conditioned on a move, and "price
kept going after a big move" is what momentum alone predicts. The null is still free — barriers
are symmetric and placed from volatility alone — but the *observed* base rate need not be 0.5,
and a result that is not separated from momentum is worth nothing. §1.7 is where that is done,
and it is the reason this document exists before any code.

Status: **registered 2026-08-23 as ledger seq 191, superseded the same day by seq 192**, and
built. Nothing has been evaluated against it: the floors in §1.9 put the earliest verdict at
~2026-11-07, and `gate accelerator` refuses until then. The supersession is described in §1.12
— two defects that the synthetic worlds of §4 caught *before* any forward data existed, and
that were registered rather than quietly applied.

---

## 1. Pre-registration (the text appended as `preregistration`, name `accelerator_stage_b`)

### 1.1 The claim, stated so it can die

> **H2.** Conditional on price entering a populated liquidation shell, the sweep **continues**
> more often when the mass lying ahead of it exceeds the mass lying behind — beyond what the
> pre-touch move alone predicts, and beyond what the arrangement of the map's mass across its
> own shells would produce by chance.

Three things must hold together; any one failing kills H2.

| | must hold | fails if |
|---|---|---|
| **signal** | forward-minus-reverse mass shifts P(continuation) off its base rate | the shift is zero, or the wrong sign |
| **substance** | the shift survives a mass permutation that leaves the touch set and labels intact | the arrangement carries nothing; it was selection |
| **mass, not momentum** | a map-blind model does **not** match the full one | the edge is the pre-touch move wearing the map's clothes |

### 1.2 The touch, defined

Fixed now, and identical to what the 2026-08-23 census counted, so the accrual number below and
the code cannot drift apart:

- Bands `B = (0.005, 0.01, 0.02, 0.05)` and shells `s_i = (B_{i−1}, B_i]`, per side, as in
  ledger seq 153 §1.2.
- The as-of map for a print is the newest snapshot that arrived **strictly earlier** and is no
  more than `MAX_MAP_AGE_NS` (5 min) old — `features.liquidations.match_slots`' rule, unchanged.
  A print with no such snapshot is not a touch and is not a miss; it is not an observation.
- A **touch** is a print whose `(side, shell)` differs from the previous print's and whose shell
  carries at least `CLUSTER_MIN_NOTIONAL` ($50k) of liquidation notional on that snapshot.
- **Debounce:** one touch per `(coin, side, shell)` per hour. Without it a price oscillating on a
  shell edge manufactures observations that are one event.
- **Sweep side** `σ = +1` if the touched shell is above the snapshot's mark, `−1` if below.

### 1.3 Fuel and brake

Both from the persisted v1 fields, cumulative from the mark, with the sign convention of
[`ATTACK.md`](ATTACK.md) §0 — mass **above** is shorts, shorts liquidate by **buying**, forced
buying pushes price **up**, so mass on the side price is sweeping toward *adds* to that sweep:

```
fuel   = (up if σ=+1 else down)["0.05"]  −  (up if σ=+1 else down)[B_i]     mass ahead, beyond the touched shell
brake  = (down if σ=+1 else up)["0.05"]                                     mass behind the mark, which fires only on a reversal
F      = (fuel − brake) / (fuel + brake)          F ∈ [−1, 1], positive = continuation predicted
```

`F = 0` (abstain) when the denominator is 0. "Brake" is named for what it does to the *sweep*,
not to price: mass behind is fuel for the reverse move, and the competition between the two is
the whole covariate.

**Deviation stated, not hidden:** `up`/`down` stop at 5%, so `fuel` is the mass between the
touched shell and 5% only — the census showed 84% of BTC's mapped mass sits beyond that
(FINDINGS, 2026-08-22). The wider `nat2.liqmap2` stream began accruing 2026-08-22 and has no
history; a bucket-level `fuel` over ±30% is a **later entry with its own 30 days**, exactly as
seq 153 §1.1 reserved for the α-kernel, and never an amendment of this one.

### 1.4 The label, and the sign trap

`labels/barriers.py::fade(path, t_touch, p_touch, sweep_side=σ, barrier_pct=k·σ_bar·√T, horizon_ns=T)`,
unchanged. It returns **+1 snap-back, −1 continuation, 0 timeout**, already expressed relative to
the sweep direction so one model learns from both.

```
y = 1  iff  fade(...) == -1        (the sweep CONTINUED)
```

Stated this baldly because the inversion is silent: `fade` is written so that +1 is the *fade*
winning, and H2 is a claim about continuation. A model trained on `y = (fade == +1)` would
produce a perfectly plausible, exactly wrong answer, and no gate would catch it.

Timeouts are **excluded**, as in Stage A: a race that never finished is a question, not a
negative answer. Barriers are placed at `±k·σ_bar·√T` from the **bar volatility alone** — never
at the next cluster, per H1 §3, or distance-to-cluster leaks into the null.

### 1.5 The grid, closed

| | values | count |
|---|---|---|
| horizon `T` | 15m, 1h | 2 |
| barrier `k` (σ) | 1, 2 | 2 |

**4 cells per coin. No cell is added after data is seen.** 15m and 1h bracket the object: the
first 197-event scan found 93% of liquidated notional arriving inside two minutes (H1 §2), so a
cascade is a minutes-scale event and a 4h or 24h horizon is a different question with its own
document.

**Universe:** coins whose `gate map` coverage is ≥ 25% at decision time — the same floor as H1,
which today means BTC, ETH and SOL. Coverage enters as an interaction, not a filter.

### 1.6 Expert, baseline, ablation

- **Expert** `magnet_b`: LightGBM depth 3 (the shipped `default_model`) over
  `fuel`, `brake`, `F`, `coverage`, `published_frac`, `map_age_s`, `sigma`, `sigma_regime`,
  `ret` (the pre-touch move — the mandatory momentum control, `CONSISTENCY.md` §7),
  `range_frac`, `tau`, `liq_flow`, and the touch's own `shell` index and `σ`.
- **Baseline** `sign(F)`, no fitting, one column: `p(continuation) = 0.5·(1 + clip(F, −1, 1))`.
  It is the `α = 0` of this family, so the comparison is nested rather than a contest between
  different objects.
- **Ablation** `magnet_b.without_map()` — the same expert with the **mass** columns removed
  (`fuel`, `brake`, `imb_fuel`), via the existing `features.spec.by_source` idiom. This is
  criterion 2 below, and it is the control the permutation cannot provide: a placebo shuffles
  map mass and leaves momentum intact, so only an ablation can show the map is carrying the
  result. `touch_shell` and `touch_sweep` stay in *both* models — see §1.12.

### 1.7 Controls

- **Purged walk-forward with embargo `T`**, uniqueness weights mandatory (`labels.barriers.uniqueness`);
  the reported `n` is the effective, non-overlapping count. Cascades produce several touches that
  one price move resolves, and counting them as independent is how this study would lie.
- **Calibration** is whatever `validate.evaluate` applies at evaluation time, to expert and
  baseline alike, on OOS folds only. Any change to that procedure is itself a registered
  amendment (the cross-fitting entry of TASK_2/11 is pending and would apply here too).
- **Mass permutation placebo**, ≥ 200 replications per cell: per snapshot, shell masses are
  shuffled across the eight `(side, band)` slots, preserving the multiset
  (`validate.placebo.permute_series`). **The touch set and the labels are held fixed** — they are
  detected on the real map and the barriers are map-independent — so the placebo asks exactly one
  question: does the *arrangement* of mass carry information? It cannot test whether the touch
  selection does; §1.6's ablation is what does that. **The statistic permuted against is the
  map's contribution** — criterion 2's full-versus-ablation margin — not the expert's total
  skill (§1.12). A cell counts as a win only at `p ≤ 0.01`.
- **Cost**, `Costs.threshold()` against the hash-stamped `costs.toml`: the OOS decision hit rate
  must exceed it. The measured round trip is ~11 bps and the n=5 fade reading in `FINDINGS.md`
  cleared nothing; an edge below cost is dead by arithmetic.

### 1.8 Decision rule, committed before the result

**CONFIRMED** requires all four:

1. Beats `sign(F)` in **≥ 2/3 of evaluated cells** (`beats_baseline`: log-loss delta z ≥ 2.0 and
   better than the constant floor — the shipped `Comparison` rule, unchanged).
2. The **map-blind ablation does not match it**: in a majority of the cells won, the full expert
   beats `without_map()` by that same rule.
3. Every winning cell's edge **collapses under the mass permutation** (p ≤ 0.01).
4. Every winning cell's OOS decision hit rate **exceeds the cost threshold**.

### 1.9 Runnable-when, measured rather than assumed

From the census of 2026-08-23 (547 touches, ~15 days, 8 coins, descriptive and not ledgered):

| | observed | at a fresh map |
|---|---|---|
| touches/day, all 8 captured coins | 36 | ~103 |
| touches/day, the 3 coins at ≥ 25% coverage | 17.9 | ~51 |
| of those, resolving inside 1h at k=1 | ~52% | ~26/day |

The observed rate was not the venue's rate: only **35% of tape time carried a fresh map**, for
reasons since fixed (FINDINGS, 2026-08-23 — the cross-section rescan and the quadratic writer
resume; snapshot cadence 434s → 62s, gaps over the staleness limit 100% → 0%).

- **Floor: 2,000 resolved touches ∧ 30 distinct UTC days**, counted forward from the seq of this
  entry, over the ≥ 25% coverage universe.
- **Earliest ≈ 2026-11-07** (76 days at ~26/day). Not the ~33 days the 8-coin figure suggests:
  five of the eight captured coins are below the coverage floor. **Widening registry coverage is
  the lever that shortens this clock** — a fact about the sweep, not about the market.
- A **cell** is read only with ≥ 200 effective (uniqueness-weighted) observations, the shipped
  `MIN_TRAIN_ROWS`; below that it is undecidable, never negative.

### 1.10 What would refute this, and what is merely undecidable

- **REFUTED — permanent:** continuation is no more likely when `F > 0`; *or* the effect survives
  the mass permutation at full strength (it was selection, not mass); *or* the map-blind ablation
  matches the full expert (it was momentum); *or* the edge is below cost in every cell it wins.
- **UNDECIDABLE — and the distinction is the point:** fewer than the floors above in a cell;
  coverage never clears 25% for enough coins; the map's ±5% span truncating `fuel` so severely
  that the covariate is uninformative — which is a fact about the *snapshot schema*, to be
  answered by the ±30% stream's own later entry, not by this one.

### 1.12 Amendment, registered as seq 192 the same day

Two defects, both found by §4's synthetic worlds **before any forward data existed**, and both
registered rather than applied silently. The gate cannot run until ~2026-11-07, so nothing was
evaluated under either version.

1. **The ablation removed the confound along with the hypothesis.** `touch_shell` and
   `touch_sweep` say where price is relative to the mark — geometry, and a proxy for the move
   that got it there. Classifying them `MAP` put them inside the ablation, so in a world whose
   continuation depends *only* on the touch bar's volatility and not on mass at all, criterion 2
   passed at **z +9.21**. With the mass-only ablation that world gives **z +1.16** and a
   planted-mass world gives **z +10.83**. They are now declared `BAR` and stay in both models.
2. **The placebo was measuring the wrong thing.** `magnet_b` reads non-map columns, so a
   permutation corrupts its baseline too and the expert-versus-baseline delta stays high for a
   reason unrelated to the claim: in the planted-mass world that statistic gave **p = 0.16**,
   as if a real effect had survived. The statistic is now the map's *contribution*, which in the
   same world has **zero exceedances in 40 replications**.

The worlds also show criterion 2 is not sufficient alone: in the pure-momentum world the
mass-only ablation still leaves **z +2.73**, because `fuel` and `brake` correlate with how far
price travelled. That world is refused by criterion 3 (**p = 0.37**), not by criterion 2 — the
conjunction is what protects the claim, which is the reason there are four criteria and not one.

### 1.11 Provenance

`source: HYPOTHESIS_1.md §2 (the threshold argument) and §3 (the free null); label
labels/barriers.fade at nat2 main 69f1e12; touch definition and accrual from the census of
2026-08-23; supersedes: null; registered_by: operator; task: TASK_2/TASKS/16`.

---

## 2. Why this is the half with the mechanism

H1's pull, if it exists, is a belief: nothing physical acts at a distance from a threshold. That
makes it fragile in a specific way — it should be stronger for visible clusters and it should
decay as the trade crowds (H1 §7). H2's continuation, if it exists, is not a belief. Forced
sellers must sell and forced buyers must buy; the flow is mechanical, its size is on the map, and
its direction is known before it happens.

The price of that is the selection problem in §1.7, and the first evidence available points the
wrong way for the *fade* side of it: the n=5 reading in `FINDINGS.md` found both material ETH
cascades **continued** — the larger by −48 and −72 bps at 15m and 60m — with no snap-back
visible. That is five
observations and settles nothing, but it is recorded, it is consistent with H2, and it was found
before this document was written rather than after.

## 3. What this unblocks, and what it ends

A CONFIRMED H2 is the first mechanically-grounded edge in the system, and the first thing that
would make an execution question worth asking. A REFUTED H2, together with a refuted H1, ends the
magnet branch entirely and routes to the informed-wallet study (`REDUCED_SPECS` §3b) — which is
why the event timeline has been capturing since 2026-08-20 regardless of how this reads.

## 4. Code task, blocked on the seq of §1

- `labels/touch.py` (≤ 120 lines): touch detection from a tick path and a map series, exactly
  §1.2; returns `(t, px, side, shell, fuel, brake, F)` per touch.
All of it built 2026-08-23, after seq 191 and with seq 192's corrections:

- `labels/touch.py` (125 lines): touch detection, §1.2 exactly.
- `experts/magnet_b.py` (165): the expert, the `sign(F)` baseline, the ablation, and the
  `y = (fade == −1)` labelling with a golden test on the sign.
- `gates/accelerator.py` (193): refusal-first, modelled on `gates/magnet.py`; `nat2 gate
  accelerator` refuses without the pre-registration, without `gate map` PASS, or below §1.9.
- `tests/test_accelerator.py` (16 tests): the unit checks, the sign trap, the gate's rule, and
  three worlds at the horizon's scale — planted mass is found and the placebo collapses it;
  pure momentum passes criterion 1 at z +9.8 and is stopped by criterion 3; a null world yields
  nothing.
- **Also fixed:** `Dataset.select()` — an ablation is a different design matrix, not just a
  shorter feature list, and every `without_map()` in the codebase would have raised at predict
  time without it. `MagnetA.without_map()` had never been exercised.
- **Budget:** zero new dependencies; `validate/`, `core/costs.py`, `experts/magnet_a.py`,
  `experts/magnet_alpha.py` and `labels/barriers.py` untouched.
