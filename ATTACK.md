# ATTACK — the function, and why it has the shape it has

Third of three. [`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) fixes what is claimed and what would refute
it; [`CONSISTENCY.md`](CONSISTENCY.md) fixes how the number is estimated. This document fixes
**the function itself**: what is computed from the map at a decision time, and why the exponents
in it are not free.

Implemented in `features/attack.py`, tested in `tests/test_attack.py`.

Status: specified and implemented. Unfitted, unvalidated, and gated behind `gate map`.

---

## 0. The sign trap, before anything else

Mass **above** the mark is short positions. Shorts liquidate by **buying**. Forced buying pushes
price **up**. So mass above the mark implies upward drift.

`LiqMap.imbalance()` is defined `(below − above) / total`, so:

```
    mu  ∝  − imbalance
```

Getting this backwards inverts every result while leaving it entirely plausible-looking, and no
gate in the system would catch it. `features/attack.py` returns positive for upward drift and
carries a test that fails if the convention moves.

---

## 1. What is being computed

An agent standing `d` away from a cluster can walk price into it. Doing so costs money. The
cascade pays money. **The function asks whether the second exceeds the first**, and it asks it for
every candidate distance at once.

This is not a scoring heuristic dressed as physics. Each term is forced by something already
committed to elsewhere in the system.

---

## 2. Construction

**Relative distance.** Everything stays in relative price units, because impact and cost both
live there and mixing them with σ-units is a silent misspecification.

```
    d_j = (p_j − M) / M
```

**Effective mass.** An isolated position is a hard point mass at a fixed price. A cross position's
liquidation level *moves* whenever anything else in that account moves, so it is soft and
discounted:

```
    m_j^eff = m_j · w_j          w_j = 1 (isolated),  omega in (0,1] (cross)
```

This is not a refinement. The first BTC map read under this design carried $76.6M at +3.56% that
was **100% cross** against $44.7M at −2.56% that was **0% cross** — summing those as comparable
masses would decide the result on its own.

**Reachable mass**, cumulative on one side:

```
    R(d) = sum of m_j^eff over positions with 0 < ±d_j <= d
```

**Displacement if it fires**, from the *same* square-root impact law that already sets the
capacity cap `Q <= q·V` in the sizing chain:

```
    delta(d) = A · sigma · sqrt( R(d) / V )          A = Y · sqrt(phi)
```

`Y` is the impact coefficient and `phi` the fraction of mapped mass that actually fires. They are
not separately identified by price data and are carried as one constant.

**The attack ratio:**

```
                 A · sigma · sqrt( R(d) / V )
    Psi* =  sup  ────────────────────────────          d* = argmax
             d          kappa · d + c
```

`Psi* > 1` means some push is profitable. `d*` names the cluster worth attacking.

---

## 3. Three things this buys that a fitted kernel does not

**The exponents are not free.** `Psi ∝ sqrt(M) / (kappa·d + c)` — mass enters at ½, distance at 1.
Both follow from the square-root impact law. `HYPOTHESIS_1.md` §4 offers `alpha in {0,1,2}` as a
free distance exponent; that grid is retained only as a **falsification of the impact law**, not
as the primary specification. If a free `alpha` beats `Psi`, the square-root law is wrong on this
venue, which is a finding worth more than the study that produced it.

**The singularity is solved rather than patched.** A kernel `M/d^alpha` explodes as `d -> 0`, and
§4 papered over it with an arbitrary floor `d_min = 0.25`. The cost term `c` is the *principled*
floor: mass sitting on the mark is not infinitely attractive, because there is no distance left to
profit from. The floor was never a free parameter — it was the round-trip cost all along, and it
is already in `costs.toml`.

**Size cancels.** Profit is `Q·delta` and cost is `Q·(kappa·d + c)`, so the attacker's size drops
out of the condition entirely. It re-enters only through capacity — you cannot unload more than
the cascade absorbs, `Q <= phi·M`, which reduces to `delta >= d` and binds *before* the profit
condition does. Hence the sharp reading:

> **the cascade must displace price further than the push had to travel.**

---

## 4. The supremum is exact, not a search

`R(d)` is a step function rising only at positions; `kappa·d + c` is strictly increasing. So
between positions the numerator is flat and the denominator grows — `Psi` can only fall. **The
maximum is therefore attained at a position.** Sort by distance, sweep a prefix sum, done in
`O(n log n)` with no grid and no approximation.

The implementation asserts this: `d*` is always a member of the observed distance set.

---

## 5. From ratio to drift — and the two hypotheses are one flag

```
    mu(s) = gamma · [ f(Psi*_up) − f(Psi*_dn) ]

    f(x) = x            passive magnet   — smooth, always on
    f(x) = (x − 1)+     hidden hand      — nothing until the push pays
```

**The hinge is what identifies `A`.** In the linear form `A` and `gamma` are confounded: rescaling
one absorbs the other, and neither is recoverable. With the hinge, `A` is pinned by *where the
kink sits* and `gamma` by the slope beyond it. The threshold is what makes the impact coefficient
estimable from price data alone — which is why the hidden-hand model is the *easier* one to test,
despite being the stronger claim.

Composed with the first-passage result:

```
    logit p(s) = ( 2·k·sqrt(T) / sigma ) · gamma · [ f(Psi*_up) − f(Psi*_dn) ]
```

with `(2k·sqrt(T)/sigma)` the known offset from `CONSISTENCY.md` §4. One free parameter `gamma`,
one profiled parameter `A`.

---

## 6. Fitting

`gamma` is linear in the logit; `A`, `kappa`, `omega` sit nonlinearly inside `Psi`. Profile, do
not jointly optimise:

```
    for A in a pre-registered grid:          # kappa = 0.5, omega = 0.5 held FIXED
        Phi_i = f(Psi_up) − f(Psi_dn)
        z_i   = (2·k·sqrt(T)/sigma) · Phi_i
        fit   logit p ~ gamma · z            # 1-D logistic, no intercept
    take argmax loglik
```

**`kappa` and `omega` are fixed a priori and this is deliberate.** Three free nuisance parameters
against this signal-to-noise is how a threshold gets manufactured. One profiled parameter is
defensible; three is a grid search wearing a theory's clothes. Moving them requires a
pre-registered grid and paying the `sqrt(2·ln N)` cost.

---

## 7. Failure modes, instrumented in advance

**A supremum is brittle.** It is upward-biased and one mispriced large position can set it. The
implementation drops the winning cluster's largest member and re-runs the sweep:

```
    concentration = 1 − Psi_jackknife / Psi*        in [0, 1]
```

1.0 means the cluster *is* one position; near 0 means it survives losing its largest member and is
a genuine crowd. On the first real registry read, BTC's downside cluster scored **100%** — the
entire $44.7M reading was a single wallet — while the upside scored 69%.

A log-sum-exp soft maximum was specified here first and **rejected after running it**: it grows
like `log(N)/tau`, so it reported position *count* rather than concentration. On real data it read
0.93 against a hard maximum of 0.15, a gap composed almost entirely of how many positions
happened to be on that side. It answered a different question than the one asked, and would have
been a permanently misleading column.

**Both sides can exceed 1.** That is a high-volatility state with fuel in both directions, not a
directional signal. `min(Psi_up, Psi_dn) > 1` is an **abstention region**, not a small net edge —
it is precisely where the mixture account says the variance lives.

**Do not rescale by coverage.** `R / coverage` is a heroic assumption about a book we do not see,
and it enters under a square root anyway. `gamma` absorbs the average level; coverage enters the
regression as an interaction so thin readings can be learned to be less informative.

**Missing is missing.** Absent `sigma` or `V` returns `None`, never `0.0`. A zero here reads as a
confident "no attack available", which is a different claim from "we could not see".

---

## 8. What it must be raced against

| | |
|---|---|
| null | `p = 0.5`, free from the symmetric barrier |
| baseline | `logit p = beta · (−imbalance(band))` — mind the sign |
| gravity | `logit p = beta · A_alpha`, `alpha in {0,1,2}` — is `sqrt(M)/d` really right? |
| placebo | the same function, mass permuted across the same locations |
| momentum | trailing return — already measured anti-persistent at 1m/5m |

Losing to *gravity* refutes the impact law. Losing to *placebo* means it was geometry. Losing to
*momentum* means it was trend-following. Only beating all five is the hypothesis.

---

## 9. State

`features/attack.py` computes `Psi*`, `d*`, the soft ratio and the drift, from positions the
registry already holds plus `sigma`, `V` and `c`. It is pure and deterministic; nothing in it
touches the network.

What it does **not** yet have: a fitted `gamma` or `A`, uniqueness-weighted samples, purged
walk-forward, isotonic calibration, or a hash-stamped `costs.toml` to draw `c` from. Until those
exist the function returns a number and that number means nothing — and until capture has accrued
the spans in `HYPOTHESIS_1.md` §8, there is nothing to fit it on.

The function was never the bottleneck. It is written down now so that it cannot be quietly
adjusted after the data arrives.
