# HYPOTHESIS_3 — the hunter: does *who* trades toward the cluster carry information?

Third claim, and the first about identity rather than mass.

[`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) asks whether mass pulls price; [`HYPOTHESIS_2.md`](HYPOTHESIS_2.md)
asks whether it accelerates a sweep once touched. Both treat the trader population as anonymous.
H3 asks the remaining question: **is there a fixed, identifiable set of wallets whose flow toward a
populated cluster predicts the touch — beyond what the map already predicts, and beyond what the
same flow from anonymous hands would?**

The idea arrives dressed as a manipulation story — "hunters push price into liquidity" — and, as
with H1's gravity metaphor, the dress is discarded before specification. Nothing here tests
intent. A wallet can precede touches because it manipulates, because it hedges cascades, because
it market-makes the approach, or because it reads the same public map we do. The claim under test
is only **statistical lead at fixed identity**: the same wallets, chosen in advance on past
behaviour, keep preceding touches out of sample. Mechanism is a question for after a PASS, never
a premise.

The venue makes the strong version of this testable at all. On HL the public tape carries **both
counterparty addresses on every print** (FINDINGS, 2026-08-08), so the complete fill flow of every
wallet is one non-user subscription — no attribution service, no entity clustering, no estimated
anything. The Arkham/on-chain-entity version of this idea is the weak version and is out of scope:
CEX and OTC footprints are invisible there, labels are third-party guesses, and both violate the
exact-or-nothing rule. If identity carries nothing *here*, where identity is exact, the cross-venue
hunt is not worth its budget.

Status: **draft — not registered.** Nothing counts until this text is appended to the ledger as a
`preregistration` entry, and per the standing rule, **no scoring code exists before that seq
does.** Every date in §8 is provisional until registration fixes it.

---

## 1. Pre-registration (to be appended as `preregistration`, name `hunter_flow`)

### 1.1 The claim, stated so it can die

> **H3.** In H1's symmetric two-barrier race, net signed flow from a **pre-identified cohort** of
> wallets, measured over a trailing window and signed toward the more-massive side, shifts
> first-passage probability — beyond the distance-weighted mass asymmetry `A_α` already in the
> race, beyond the pre-race move, and **beyond the same flow statistic computed from
> activity-matched wallets outside the cohort.**

The last clause is the hypothesis. Aggregate taker flow trivially precedes price — flow *is*
impact — so a cohort defined as "wallets that trade a lot" would pass a naive test by arithmetic.
H3 is a claim about **identity**: that knowing *which* wallets are trading adds information that
the same volume from anonymous hands does not carry.

Three things must hold together; any one failing kills H3.

| | must hold | fails if |
|---|---|---|
| **signal** | frozen-cohort flow shifts first-passage probability, at the map, beyond `A_α` | the shift is zero, or the wrong sign |
| **identity** | the cohort beats activity-matched placebo cohorts | matched anonymous flow does the same work; it was impact, not identity |
| **persistence** | membership chosen on `[t−90d, t]` carries the effect through `(t, t+30d]` | the cohort must be re-picked to keep working; it was selection |

The second is this document's permutation placebo — the control most likely to be skipped and the
one that separates a claim from an anecdote.

### 1.2 The race, unchanged

The observation unit is **H1's race, verbatim**: barriers at `±k·σ·√T` placed from volatility
alone (never at a cluster — H1 §3), label = which barrier is touched first, timeouts excluded,
uniqueness weights mandatory, reported `n` is the non-overlapping count. H3 adds covariates to an
existing label; it invents no new one. A race is **in-sample for H3** only when at least one side
carries ≥ `CLUSTER_MIN_NOTIONAL` ($50k, H2 §1.2's floor) of mapped mass inside the barrier span —
cohort flow "toward liquidity" is undefined where there is no liquidity to be toward.

### 1.3 Cohort formation, frozen before scoring

Formation mirrors the persistence gate's split exactly: behaviour in `[t−90d, t]` selects, and
only `(t, t+30d]` scores. Membership is **frozen at each monthly rebalance** and the freeze is a
ledger entry (`cohort_freeze`, listing the addresses and the formation statistic) *before* the
scoring window opens. A cohort adjusted after seeing its scoring window is not a cohort; it is a
fit.

**Formation statistic**, per wallet, over the formation window:

```
lead(w) = Σ_touches  signed_flow(w, touch − W, touch) · 1[flow toward touched side]
          ───────────────────────────────────────────────────────────────────────
          Σ_all_windows |signed_flow(w, ·)|
```

— the fraction of a wallet's tape-reconstructed signed flow that lands in the `W` minutes before
a touch (H2 §1.2's touch, reused verbatim) *and* on the side the touch resolved toward,
normalised by the wallet's total activity so a wallet that simply trades constantly scores 0.5ish,
not high. Wallets below `MIN_FORMATION_NOTIONAL` ($1M formation-window volume) are excluded —
below that, `lead(w)` is noise. The cohort is the **top 50 wallets by `lead(w)`** meeting the
floor; 50 is fixed now and does not move.

**Vault addresses are excluded** at formation (as everywhere in the system): HLP absorbs forced
flow by construction, and a cohort that rediscovers the backstop liquidator has discovered the
venue's plumbing, not information. The 2026-08-09 finding — 16 of 70 sampled wallets had taken
the counterparty side of a liquidation — says the absorber population is real and identifiable;
whether *absorbing* flow (which is reactive) also *precedes* touches is left to the data, not
assumed either way.

### 1.4 The covariate

Per race, from tape-reconstructed cohort fills only:

```
H = signed cohort net flow over the trailing window W before race start,
    signed + toward the barrier whose side carries more mapped mass (A_α's sign),
    scaled by the same liquidity block as m_b (book_thin, amihud)
```

`H = 0` (abstain) when the cohort traded nothing in the window or the map is balanced. Scaling by
depth for the same reason mass is: $10M of cohort flow into a thin book and into a deep one are
different events.

### 1.5 Expert, baseline, ablation

- **Expert** `hunter_a`: the shipped `default_model` (LightGBM depth 3) over `H`, `A_α` (at H1's
  registered kernels), `imbalance`, `coverage`, `map_age_s`, `sigma`, `sigma_regime`, `ret` (the
  mandatory momentum control), `range_frac`, and the race's `k`, `T`.
- **Baseline**: the **full H1 expert for that cell** — whatever H1's evaluation crowns, or
  `sign(imbalance)` while `gate magnet` is unresolved. H3 must add to the map, not rediscover it.
- **Ablation** `hunter_a.without_cohort()`: `H` removed, everything else kept, via
  `features.spec.by_source` — the H2 §1.12 lesson applied from the start: only the hypothesis
  column is classified `COHORT`; geometry and momentum stay in both models.

### 1.6 The grid, closed

| | values | count |
|---|---|---|
| horizon `T` | 1h, 4h | 2 |
| barrier `k` (σ) | 1, 2 | 2 |
| flow window `W` | 30m, 4h | 2 |

8 cells. **No cell is added after data is seen.** The 24h horizon, per-wallet (rather than
cohort-aggregate) scoring, and any cross-venue extension are later entries with their own floors,
never amendments of this one. **Universe:** coins at ≥ 25% `gate map` coverage at decision time,
coverage as an interaction — same floor, same reason as H1 and H2.

### 1.7 Controls

- **Activity-matched placebo cohorts, ≥ 200 replications per cell.** Each replication draws 50
  non-cohort wallets matched to the cohort on formation-window volume decile and trade count
  decile, recomputes `H` from their fills, and re-runs the full evaluation. The statistic
  permuted against is **the cohort column's contribution** — the full-versus-ablation margin,
  H2 §1.12's correction adopted from the start, never the expert's total skill. A cell counts
  as a win only at `p ≤ 0.01`. This placebo is the identity criterion and is not optional.
- **Time-shuffle placebo**: cohort membership kept, each wallet's flow series circularly shifted
  by a random offset ≥ 7 days. Kills any construction that leaks the label into `H` through
  alignment rather than behaviour. Must collapse the effect; reported, not gating.
- **Self-impact bound, reported per cell**: the cohort's own flow moves price, so some lead is
  mechanical. The placebo already prices matched *volume*; additionally report median cohort flow
  in `W` as a fraction of tape volume in `W`. A "signal" from a cohort that *is* a third of the
  tape is execution reading, not information, and the number is on the card either way.
- **Purged walk-forward with embargo `max(T, W)`**, uniqueness weights, calibration via
  `validate.evaluate` on OOS folds only — all unchanged from H1/H2. The **cohort freeze is part
  of the purge**: a fold may only score races whose cohort was frozen before the fold's start.
- **Cost**, `Costs.threshold()` against the hash-stamped `costs.toml`, funding hourly and charged.

### 1.8 Decision rule, committed before the result

**CONFIRMED** requires all five:

1. Beats the H1-expert baseline in **≥ 6/8 cells** (the shipped `Comparison` rule: log-loss delta
   z ≥ 2.0 and better than the constant floor).
2. The **cohort-blind ablation does not match it** in a majority of cells won, by the same rule.
3. Every winning cell's cohort contribution **collapses under the activity-matched placebo**
   (p ≤ 0.01).
4. The effect holds across **≥ 3 consecutive monthly rebalances** with frozen membership — the
   persistence criterion, and the one that distinguishes a cohort from a lucky quarter.
5. Every winning cell's OOS decision hit rate exceeds the cost threshold.

**REFUTED — permanent, do not reopen:**

- Matched placebo cohorts do the same work. Identity carries nothing; flow was just impact, and
  the tradeable residue, if any, already belongs to H1's covariates.
- The ablation matches the full expert: `H` added nothing over the map and the move.
- The effect exists only for the formation window's own touches and dies at every rebalance:
  selection, dressed as skill.
- The edge is below cost in every cell it wins. Dead by arithmetic; check first.

**UNDECIDABLE — and the distinction is the point:**

- Fewer than the §8 floors in a cell; coverage never clears 25% for enough coins; or fewer than
  50 wallets ever clear `MIN_FORMATION_NOTIONAL` — a fact about the venue's population, not the
  market, and it routes to a redesign entry, not a verdict.

### 1.9 Fingerprints, descriptive and not gating

If a PASS is manipulation-shaped rather than shared-signal-shaped, it should show: cohort flow
concentrated in the minutes before the touch rather than spread across `W`; cohort *exit* on the
far side of the sweep (fills reversing inside H2's continuation window); and effect size rising
with cluster visibility (H1 §7's prominence measure). None of these decide H3 — they interpret
it, and they are the entire content of any later manipulation question, which is otherwise not
this system's to answer.

### 1.10 Provenance

`source: HYPOTHESIS_1.md §3 (the race and the free null), HYPOTHESIS_2.md §1.2 (the touch, the
$50k floor, the §1.12 ablation/placebo corrections adopted a priori); tape counterparty finding
FINDINGS 2026-08-08; formation split mirrors gate persistence (DESIGN.md); supersedes: null;
registered_by: operator; task: unassigned`.

---

## 2. Why the confound here is worse than H1's and H2's

H1's confound is geometry; H2's is momentum. H3's is **impact**, and it is nastier because it is
not a covariate to control but the covariate itself wearing a mask: flow predicts price *because
flow moves price*, at every window, for every wallet, mechanically. A test without §1.7's
matched-cohort placebo would confirm H3 on any tape ever recorded, and the confirmation would be
a restatement of the definition of a market order.

That is why the claim is phrased as identity-minus-impact, why the placebo matches on activity
and not just count, and why criterion 3 permutes the cohort column's contribution rather than
the expert's skill. What survives all three is the only thing worth the name: **information in
the names.**

## 3. What this unblocks, and what it ends

CONFIRMED gives the smart-money branch its first market claim (persistence, if it passes, says
skill exists; H3 says it is *legible in advance*), hands `gate decay` a concrete cohort whose
alpha half-life is worth measuring, and makes the cross-venue/Arkham extension worth a document.
REFUTED — identity carries nothing at the one venue where identity is exact — closes the hunter
idea at every venue where identity is worse, which is all of them, and the refusal is the
product.

## 4. Prerequisites

| | state | blocks |
|---|---|---|
| ledger registration of §1 | **not done — first action** | all scoring code |
| `gate feed` PASS | done | everything |
| tape capture with counterparties, continuous | running since 2026-08-20 | formation windows |
| per-wallet signed-flow reconstruction from tape | position reconstruction merged (task 07); per-wallet flow aggregation **not built** | `lead(w)`, `H` |
| touch series (H2 §1.2) | built | formation statistic |
| race labels + purged WFO + uniqueness | built (H1/H2 machinery) | evaluation |
| `cohort_freeze` ledger entry type | **not built** | §1.3 |
| activity-matched placebo sampler | **not built** | criterion 3 |
| `costs.toml`, hash-stamped | shared with H1/H2 | criterion 5 |

## 5. Power, and what the clock actually permits

The race supply is H1's (~240 non-overlapping 1h obs/day across a 10-coin universe, H1 §8), cut
by the populated-side condition of §1.2 — assume half survive, so the observation floor is not
the binding clock. The binding clock is **formation**: the first 90-day window closes ~90 days
after capture began (2026-08-20 → **~2026-11-18**), the first frozen cohort scores its 30-day
window through **~2026-12-18**, and criterion 4's three consecutive rebalances put the earliest
full verdict at **~2027-02-18**. A first *partial* read (criteria 1–3, 5 on one scoring window)
is available ~2026-12-18 and is explicitly labelled partial in any card that shows it.

**Floors:** per cell, ≥ 2,000 non-overlapping in-sample races spanning ≥ 30 distinct UTC days
(H1's floor), and ≥ 200 effective observations per fold (`MIN_TRAIN_ROWS`); for criterion 4,
three complete freeze→score cycles. Below any floor: undecidable, never negative.

Capture accrues calendar time and nothing recovers a day not captured. As with H1: that is the
only thing on this page that is urgent.
