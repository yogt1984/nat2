# CONSISTENCY — how the drift field is estimated, and what makes it identifiable

Companion to [`HYPOTHESIS_1.md`](HYPOTHESIS_1.md). That document fixes *what is claimed* and
*what would refute it*. This one fixes *how the number is produced* — the estimator, the
constraint that makes it identifiable, and the baselines it has to beat.

The name is the argument. The signal in this problem is far too small to be read off returns
directly, so power has to come from somewhere other than sample size. It comes from **internal
consistency**: the theory says how the effect must scale across barriers, horizons and coins, and
imposing that scaling turns eighteen weakly-estimated coefficients into one strongly-estimated
parameter. A model that fits each cell freely and merely *looks* consistent afterwards has
established nothing.

Status: specified, not built.

---

## 1. The object is a drift field, not trailing momentum

What is being learned is the conditional drift as a function of map state:

```
    mu(s) = lim_{h->0}  (1/h) · E[ X_{t+h} − X_t  |  S_t = s ]
```

`X` is log mark price and `s` is the state: map configuration, liquidity, volatility, coverage.
Everything else — first-passage probability, trade direction, position size — is derived from
`mu`.

**Trailing return is a control, not the signal.** It enters `s` as a covariate so that a learned
function which is secretly trend-following is exposed as such. On this venue that control has a
known prior sign: 1m and 5m bar returns were measured *anti*-persistent, 34 of 36 cells negative.
A magnet that turns out to be short-horizon momentum is not a magnet, and it is already refuted.

---

## 2. Do not regress returns

Over horizon `h`, `r = mu·h + sigma·sqrt(h)·eps`, so the best achievable fit is

```
    R^2  ~  mu^2 · h / sigma^2
```

which at realistic drift-to-vol ratios sits around 1e-4. A regression of returns on features is
dominated by a handful of tail moves, and the fitted coefficient is a report on those moves rather
than on the state. This is the estimator to avoid, and it is the obvious one.

---

## 3. Learn the probability, invert to the drift

For Brownian motion with drift and symmetric absorbing barriers at `±a`, first passage is exactly
logistic:

```
    P(hit +a first)  =  1 / (1 + exp(−2·mu·a/sigma^2))  =  logistic(2·mu·a/sigma^2)
```

With `a = k·sigma·sqrt(T)` this collapses to

```
    logit p(s)  =  2·k·sqrt(T) · mu(s)/sigma(s)

    mu_hat(s)   =  sigma(s) · logit(p_hat(s)) / (2·k·sqrt(T))
```

So the procedure is: **fit a classifier on the two-barrier race label, then invert.** Logistic
regression is not a convenient default here — under this model it is the correctly specified one,
and the linear predictor *is* the drift in disguise.

Two honest caveats, both of which belong in any write-up of a result:

**Under Gaussian returns this is not the efficient estimator.** The sample mean would be. The
barrier label discards magnitude information and pays for it. It wins here because returns are
emphatically not Gaussian — under the tails actually observed, a path-and-sign statistic has much
better finite-sample behaviour than a mean. This is a robustness argument, not an efficiency one,
and it should not be reported as the latter.

**`mu_hat` is a path-averaged effective drift, not an instantaneous one.** If the true drift rises
sharply as price approaches mass — which the hidden-hand reading predicts — then `mu_hat` is a
smeared version of it. Adequate for sizing a trade; not adequate for a claim about the shape of
the field near `d = 0`.

---

## 4. The consistency constraint — where the power comes from

Substituting `mu = gamma·Psi` gives a coefficient that is *not* free to vary across cells:

```
    logit p  =  ( 2·k·sqrt(T) / sigma ) · gamma · Psi

    beta_{k,T}  =  2·gamma·k·sqrt(T)/sigma          for every cell
```

The eighteen cells of `HYPOTHESIS_1.md` §4 therefore share **one** parameter. `(2k·sqrt(T)/sigma)`
is a known offset computed per observation, not something to estimate.

**Primary estimator — one parameter, every observation:**

```
    logit p_i  =  gamma · [ 2·k_i·sqrt(T_i)/sigma_i ] · Psi_i
```

**Then relax and test.** Let `gamma` vary by cell and compare out-of-sample log loss against the
pooled fit. If the relaxation does not improve OOS, the constraint holds and it bought a large
amount of statistical power for nothing. If it does improve, the structural model is wrong — which
is worth knowing at a fraction of the cost of discovering it later.

This nested comparison is a stronger test than any of the per-cell significance tests it replaces.
Eighteen coefficients that are individually significant but do not obey `beta ∝ k·sqrt(T)/sigma`
are eighteen ways of fitting noise, and only the joint restriction can say so.

---

## 5. Three tiers, and the comparison between them is the result

```
    tier 1   structural       mu = gamma·Psi                    1 parameter
    tier 2   single-index     mu = g(theta'z), g monotone       index + a free shape
    tier 3   nonparametric    LightGBM depth 3                  flexible
```

**Tier 1 first**, because one parameter can actually be established.

**Tier 2 does double duty and is the one to build first after tier 1.** A monotone but otherwise
unconstrained link `g`, fitted by isotonic regression or a shape-constrained GAM, lets the data
report the *shape* of the response while the index stays tight. That shape is the discriminator
between the two readings of the hypothesis: a smooth `g` is the passive-anticipation account, a
**step at `Psi = 1`** is the profitable-push account. Estimator and scientific test are the same
object.

**Tier 3 is a ceiling, not a deliverable.** It bounds achievable skill. If it fails to beat tier 1
out of sample, that is a result: the structural model captured what is there, and the
one-parameter version ships with far more confidence than an ensemble could justify. Depth 3, as
elsewhere — short history punishes variance.

---

## 6. Sample construction, where most of the damage happens

| | rule | why |
|---|---|---|
| **uniqueness weights** | weight each race by the inverse of concurrent overlapping races | every bar starts a race; unweighted overlap inflated a sibling result from 0.06 to 0.39 |
| **block bootstrap** | resample by **map snapshot**, not by bar | the map refreshes ~6h, so `Psi` is a step function and consecutive rows share a feature value exactly |
| **as-of only** | map from `io/mapsnap` as believed at `t` | rebuilding from today's map is the lookahead the persistence layer exists to prevent |
| **pooling** | pool coins, normalize within coin, **no coin fixed effects** | fixed effects absorb the between-coin mass-vs-depth variation, which is where the signal lives |
| **purged WFO** | embargo = `T`, purge overlapping training rows | overlapping cascade episodes are the standing overfit risk |

---

## 7. Baselines, in order of how bad it would be to lose

1. `p = 0.5` — the null, free from the symmetric barrier.
2. `sign(imbalance)` — the mandated baseline, and the `alpha = 0` special case.
3. **trailing return** — the actual momentum control. Non-negotiable.
4. **the permutation-placebo map** — geometry with mass shuffled across the same locations.

Losing to (3) means the learned function is trend-following. Losing to (4) means it is
support-and-resistance. Neither is the hypothesis.

---

## 8. Evaluation — proper scoring, and expect to trade a subset

Accuracy is the wrong metric and will hide the result in either direction.

- **Out-of-sample log loss and Brier**, against all four baselines.
- **Calibration curve**, isotonic fitted on OOS folds only.
- **P&L as a function of decision threshold**, with the trade count at each — never one number.

The framing is **selective prediction**. If the push account is right, most states carry no edge
and a few carry a large one; the model's job is to find the tail of `|logit p_hat|`, not to be
right on average. A calibrated classifier at 51.5% overall accuracy can be worth a great deal, and
an uncalibrated one at 55% can be worth nothing.

---

## 9. From the drift to a position

```
    w  =  mu_hat(s) / (lambda · sigma^2)
```

then through the existing chain — vol target, capacity `Q <= q·V`, HL `maxLeverage`,
liquidation-distance floor — with the trade gated on `mu_hat · T > 2c`, the same cost arithmetic
applied per state rather than on average.

The consistency worth noting: `Psi` and the capacity cap are derived from the *same* square-root
impact law, so signal and size constraint come from one model rather than two that happen to
coexist.

---

## 10. Failure modes to instrument in advance

**Volatility regime will dominate if sigma-normalization is even slightly wrong.** High-vol
periods carry both larger map displacement and larger returns, and the result will be a vol-timing
model wearing the magnet's name. Instrument: check that `mu_hat/sigma` is stable across vol
quintiles, and report it whether or not it is.

**The 6h snapshot cadence caps resolution.** The push account predicts drift changing sharply on
approach, but between snapshots the map is frozen. Intra-snapshot dynamics are unreachable until
tape-based reconstruction fills the gap — one more reason `wallets replay` matters more than its
size suggests.

**A step at `Psi = 1` is exactly the shape a threshold artefact produces.** Before believing it,
confirm the break survives the permutation placebo and does not coincide with a bin edge, a
coverage cliff, or a coin entering the universe.

---

## 11. What exists

| | state |
|---|---|
| two-barrier race labels | built (`labels/barriers.py`) |
| as-of feature frame | built (`features/frame.py`) |
| map snapshots | built (`io/mapsnap.py`) |
| `imbalance` baseline | built (`features/liqmap.py`) |
| `Psi`, impact-scaled mass | **not built** |
| uniqueness weights | **not built** |
| purged WFO + isotonic calibration | **not built** |
| `costs.toml`, hash-stamped | **not built** |

Four missing pieces, none large. The estimator is not the bottleneck — the running capture daemon
is, and nothing on this page produces a number until it has been accruing for the spans named in
`HYPOTHESIS_1.md` §8.
