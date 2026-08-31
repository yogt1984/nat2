# HYPOTHESIS_4 — the prophet: are some wallets positioned before big moves, persistently?

Fourth claim, and the persistence gate's market-facing form.

[`HYPOTHESIS_3.md`](HYPOTHESIS_3.md) asks whether identified *flow toward clusters* predicts the
touch. H4 drops the map entirely and asks the general question: **is there a fixed set of wallets
that is on the right side of large moves before they happen — not once, which is luck, but out of
sample and across rebalances, which is information?** The two share machinery and discipline but
are separate hypotheses about possibly separate populations: a wallet good before liquidation
sweeps and a wallet good before macro-driven moves need not be the same animal, and neither
document is allowed to borrow the other's verdict.

H4 is also the sharp form of `gate persistence` (DESIGN.md M3). The gate as designed asks whether
*PnL* persists; PnL conflates sizing, leverage and luck. Pre-positioning before a move is the
cleaner statistic — closer to the thing actually sought, which is evidence a wallet knew (or
computed) something before the tape showed it. If H4 is CONFIRMED, `gate persistence` has its
verdict with a better instrument; if REFUTED, the gate's question is answered in the negative for
the strongest available formation statistic, which is more informative than answering it for the
weakest.

The population trap is named before anything else, because it decides the design. **The
leaderboard holds 41,392 wallets** (FINDINGS, 2026-08-07). If a formation window contains ~30 big
moves, a coin-flip wallet sides correctly on 75% of them with probability ~0.005 — so pure chance
manufactures **~200 wallets** at that hit rate before any information exists anywhere. Ranking by
the statistic and admiring the top of the list is a multiple-comparisons machine, and every
criterion in §1.8 exists because of this paragraph.

Status: **draft — not registered.** Nothing counts until this text is appended to the ledger as a
`preregistration` entry, and no scoring code exists before that seq does. Dates in §8 are
provisional until registration fixes them.

---

## 1. Pre-registration (to be appended as `preregistration`, name `prophet_positioning`)

### 1.1 The claim, stated so it can die

> **H4.** Wallets selected on pre-positioning skill in `[t−90d, t]` are pre-positioned on the
> correct side of large moves in `(t, t+30d]` more often than activity-matched wallets are —
> beyond what a public-price momentum rule achieves on the same moves, and across consecutive
> rebalances with membership frozen in advance.

Three things must hold together; any one failing kills H4.

| | must hold | fails if |
|---|---|---|
| **signal** | the frozen cohort's OOS pre-positioning hit rate exceeds the matched-placebo distribution | the cohort is indistinguishable from matched chance |
| **not momentum** | the cohort beats a momentum rule fed the same public prices | "prescience" was trend-following, detectable for free from the tape |
| **persistence** | the effect survives ≥ 3 consecutive frozen rebalances | the cohort must be re-picked to keep working; it was selection |

The second is the control specific to this claim: a simple trend-follower is "consistently on the
right side of big moves" in any trending regime, and a cohort of momentum bots is a strategy
detected, not information found — a different and cheaper-to-copy object, recorded if seen but
never confirmed as H4.

### 1.2 The big move, defined map-blind

A **move** is a `(coin, t₀, direction)` event: over horizon `T` from bar close `t₀`, the price
path first reaches `±k·σ·√T` (the same barrier construction as H1's races, volatility-scaled,
map never consulted), with:

- **Debounce:** one move per `(coin, direction)` per `T` — overlapping triggers are one event,
  exactly the uniqueness lesson of H1 §4.
- The move set is computed from mark prices alone and is **identical for cohort, placebos and the
  momentum rule** — the event definition is shared or the comparison is meaningless.

| | values | count |
|---|---|---|
| horizon `T` | 4h, 24h | 2 |
| threshold `k` (σ) | 2, 3 | 2 |

4 cells. **No cell is added after data is seen.** Intraday (`T` < 4h) prescience is H3's
territory via the touch; multi-day is a later entry. **Universe:** all coins under capture — H4
needs no map, so the 25% coverage floor does not apply; the liquidity block still excludes coins
where position reconstruction is unreliable (builder-deployed perps out by default, as
everywhere).

### 1.3 Pre-positioning, defined

For wallet `w` and move `(coin, t₀, dir)`:

```
pos(w, t₀)      = tape-reconstructed signed position in coin at t₀ (task 07 machinery)
Δpos(w)         = pos(w, t₀) − pos(w, t₀ − L)          the position BUILT in the lookback
aligned(w, mv)  = sign(Δpos(w)) == dir  and  |Δpos(w)| ≥ MIN_BUILD ($100k notional)
```

Lookback `L = 24h`, fixed. The statistic is the **built** position, not the held one: a wallet
long BTC for six months is aligned with half of all upward moves by inertia, and inertia is not
positioning. `MIN_BUILD` floors out dust rebalances. A wallet with no qualifying build for a move
is **abstaining** on that move — abstentions are excluded from its hit rate, and a wallet must
participate in ≥ 10 moves in a window to be scored at all (below that, its hit rate is noise).

**Timing granularity is recorded, not gated:** the build's time-of-arrival relative to `t₀`
(median lead, in hours) is on every card. A cohort whose builds land seconds before `t₀` is
starting moves, not foreseeing them — that is §2's impact confound, and it is answered
descriptively here and decisively by the placebo in §1.7.

### 1.4 Cohort formation, frozen before scoring

Identical discipline to H3 §1.3: formation on `[t−90d, t]`, monthly rebalance, membership frozen
via a `cohort_freeze` ledger entry **before** the scoring window opens.

**Formation statistic**, per wallet meeting the participation and `MIN_FORMATION_NOTIONAL` ($1M
window volume) floors:

```
prophet(w) = shrunk hit rate = (hits + 5) / (participations + 10)
```

— a Beta(5,5) shrink toward 0.5, so a 3-for-3 wallet (0.65 shrunk) ranks below a 25-for-30 wallet
(0.75 shrunk). The shrink is the first, crude answer to the ~200-false-prophets arithmetic: raw
hit rates rank tiny samples first, and tiny samples are where chance lives. The cohort is the
**top 50** by `prophet(w)`; 50 fixed, vaults excluded at formation, both as in H3.

### 1.5 The momentum yardstick

`mom(mv)`: sign of the trailing `L`-window return at `t₀ − ε`, same `L`, same move set, computed
from public mark prices only. Its hit rate on the scoring window's moves is the **not-momentum
bar**: criterion 2 requires the cohort's OOS hit rate to exceed `mom`'s on the same moves. No
fitting, one column, free — the `sign(imbalance)` of this family.

### 1.6 Scoring

Per scoring window and cell: cohort OOS hit rate over aligned/participated moves, each move
counted once per wallet, wallets equal-weighted (notional-weighting hands the result to one
whale, and one whale is an anecdote with a balance). Reported `n` is wallet-moves after debounce.
No model is trained: **H4's evaluation is a comparison of proportions, not an expert.** An expert
over cohort features is downstream work that only a PASS unblocks (§3).

### 1.7 Controls

- **Activity-matched placebo cohorts, ≥ 200 replications per cell** — H3 §1.7's construction
  verbatim: 50 non-cohort wallets matched on formation-window volume decile and trade-count
  decile, full pipeline re-run, `p ≤ 0.01` to count a win. This is simultaneously the
  multiple-comparisons answer (the placebo cohorts are drawn from the same 41k-wallet pool and
  carry the same selection luck) and the impact answer (matched wallets move price too).
- **Time-shuffle placebo**: membership kept, each wallet's fill series circularly shifted ≥ 7
  days, move set fixed. Kills alignment-by-construction leaks. Must collapse; reported.
- **The momentum yardstick**, §1.5 — gating, criterion 2.
- **Survivorship, handled by construction:** scoring uses only wallets alive at freeze time, and
  a cohort wallet that blows up mid-window **stays in the denominator** — its subsequent
  abstentions are genuine zeros of opportunity, and dropping it is the survivorship bias
  re-imported. Nothing before capture start (2026-08-20) is scanned, ever: the leaderboard's
  backward history is survivor-only and is not data.
- **No walk-forward machinery is needed** — the freeze/score split *is* the purge — but scoring
  windows never overlap formation windows of any later freeze that scores them, and the ledger
  ordering enforces it.

### 1.8 Decision rule, committed before the result

**CONFIRMED** requires all four:

1. Cohort OOS hit rate beats the matched-placebo distribution at `p ≤ 0.01` in **≥ 3/4 cells**.
2. Cohort OOS hit rate exceeds the momentum yardstick's in every cell won.
3. Both hold across **≥ 3 consecutive frozen rebalances** (each rebalance's scoring window
   evaluated on its own frozen membership; a majority of windows must individually clear 1 and 2).
4. The implied edge clears cost: a follower entering at the cohort's median detection lead, at
   `costs.toml` rates with hourly funding, retains positive expectancy on the scored moves. H4 is
   not a trading rule, but a "prescient" cohort whose lead is too short to follow is a fact about
   latency, and criterion 4 is what separates a finding from a headline.

**REFUTED — permanent, do not reopen:**

- Matched placebos match the cohort. The top of the leaderboard is the top of a chance
  distribution, and every whale-watching product built on ranking it is priced accordingly.
- The cohort's edge is the momentum rule's edge. Strategy detected, not information; noted in
  FINDINGS, branch closed.
- The effect never survives a rebalance. Selection, dressed as skill.
- The edge exists and is unfollowable at any reachable cost. Dead by arithmetic.

**UNDECIDABLE — and the distinction is the point:** fewer than the §8 floors; fewer than 50
wallets ever clear the participation floors (a fact about the population, routed to a redesign
entry); or the move set in a cell is too thin because the window was quiet — a fact about the
regime, not the market.

### 1.9 Fingerprints, descriptive and not gating

A PASS worth building on should show: leads of hours, not seconds (§1.3's timing card); hit-rate
stability across coins rather than one lucky market; and cohort overlap across rebalances well
above the placebo cohorts' overlap — the same names recurring is the entire content of the word
"persistent". Cohort overlap with H3's cluster-flow cohort is reported with interest and no
weight: same names in both would be the most useful sentence either study could produce, and it
must be earned by both independently.

### 1.10 Provenance

`source: HYPOTHESIS_3.md §1.3/§1.7 (freeze discipline, matched placebo) adopted verbatim;
HYPOTHESIS_1.md §4 (barrier construction, uniqueness); position reconstruction task 07
(93b65ac); population arithmetic from FINDINGS 2026-08-07 (41,392 wallets); sharpens: gate
persistence (DESIGN.md M3); supersedes: null; registered_by: operator; task: unassigned`.

---

## 2. Why the confound here is chance itself

H1's confound is geometry, H2's is momentum, H3's is impact. H4 faces all of momentum and impact
— §1.5 and the matched placebo carry those — but its defining confound is cheaper than any of
them: **at n = 41,392, extreme order statistics of a fair coin are indistinguishable from skill
in any single window.** The design's whole answer is that chance does not *persist*: a wallet
drawn from the tail of a chance distribution regresses to 0.5 the moment the window that selected
it closes, and criterion 3 is therefore not a robustness check but the hypothesis itself. One
scoring window of H4 proves nothing and is labelled partial wherever it appears; three frozen
rebalances is the minimum sentence in which the word "persistent" means anything.

## 3. What this unblocks, and what it ends

CONFIRMED gives `gate persistence` its PASS with the strongest available instrument, promotes the
cohort to `smart_flow`'s input (M4), and hands `gate decay` exactly what it needs: a named cohort
whose alpha half-life — under the crowding that HL's public leaderboard guarantees, `/whales`
being the crude public version of this very scan — is the number that decides copyability.
REFUTED closes the smart-money branch's strongest form: if pre-positioning skill does not persist
at the one venue where positions are exact, PnL-ranked whale-following is a costume, and the
branch ends with the refusal as the product.

## 4. Prerequisites

| | state | blocks |
|---|---|---|
| ledger registration of §1 | **not done — first action** | all scoring code |
| `gate feed` PASS | done | everything |
| tape capture, continuous | running since 2026-08-20 | formation windows |
| per-wallet position series from tape | reconstruction merged (task 07); windowed `Δpos` aggregation **not built** | §1.3 |
| move-set builder (map-blind barriers, debounce) | H1 barrier code reusable; event scan **not built** | §1.2 |
| `cohort_freeze` ledger entry type | shared with H3, **not built** | §1.4 |
| activity-matched placebo sampler | shared with H3, **not built** | criterion 1 |
| `costs.toml`, hash-stamped | shared with H1/H2 | criterion 4 |

## 5. Power, and what the clock actually permits

The binding clock is H3's, shifted by nothing: formation needs 90 captured days (2026-08-20 →
**~2026-11-18** for the first freeze), the first scoring window closes **~2026-12-18**, and
criterion 3's three rebalances put the earliest full verdict at **~2027-02-18**. The move supply
is the secondary floor: at k=2σ/4h, order tens of moves per coin-month across the captured
universe — thousands of wallet-moves per scoring window, comfortably above the floors below,
unless the regime goes quiet, which is §1.8's undecidable and not anyone's failure.

**Floors:** per cell and scoring window, ≥ 300 debounced wallet-moves across ≥ 15 distinct UTC
days; per wallet, ≥ 10 participations to be scored; for criterion 3, three complete freeze→score
cycles. Below any floor: undecidable, never negative.

Same closing sentence as H3, because it is the same fact: capture accrues calendar time, nothing
recovers a day not captured, and that is the only urgent thing on this page.
