# HYPOTHESIS_5 — the cascade: can forced flow be detected as it starts, and predicted before it does?

Fifth claim, and the first that drops the magnet framing entirely.

[`HYPOTHESIS_1.md`](HYPOTHESIS_1.md) asked whether mass *pulls* price (reflexive, a belief about
beliefs). [`HYPOTHESIS_2.md`](HYPOTHESIS_2.md) asked whether, once touched, mass *carries* price
(mechanical). H5 asks the question the other two circle: **is a liquidation cascade an event
with a detectable onset and a predictable approach, and does the visible map add anything to
the prediction beyond what the tape's own recent liquidations and volatility already say?**

The word "predict" is held to one meaning here, the information coefficient: rank correlation,
out of sample, between a forecast made at `t` and a quantity realized after `t`. Not a
classifier, not a P(cascade), not a strategy. A model is built only after the IC exists, and
§1.9 is the gate between the two.

Status: **draft — not registered.** Nothing counts until this text is appended to the ledger as
a `preregistration` entry, and no scoring code exists before that seq does.

---

## 0. The fact that shapes the design

Measured 2026-10-04 on `registry.sqlite`, BTC, realized liquidations since the map tape began
(2026-08-07, 58 days), summed per 5-minute bin:

| | count |
|---|---|
| bins with any liquidation | 404 |
| bin notional p50 / p90 / p99 | $51k / $473k / $3.0M |
| bins ≥ $1M | 16 |
| bins ≥ $5M | 3 |

The three ≥ $5M bins (08-20 06:45, 08-21 00:15, 08-21 21:20; $28–31M each) moved mark by 0.08%,
0.38% and 0.16% inside the bin. They were one large wallet each, not a chain. **A cascade in the
sense this document means — many wallets, price displaced, flow feeding flow — has happened at
most a handful of times on this tape**, and "a handful" is not a sample. Any design that labels
cascades as binary events and trains on them is undecidable for a year or more, and would say
so at every evaluation. So:

- **Detection** (§1.2) is a *definition*, applied after the fact, and is used only to describe
  episodes and to check that the continuous target below is measuring the right thing.
- **Prediction** (§1.3–1.8) targets a *continuous* quantity — forward realized liquidation
  intensity — of which there are ~83,000 one-minute observations in 58 days per coin. That is
  where an IC can be measured at all.
- **The model** (§1.9) is gated on the IC, and is a perceptron only in the sense that it is the
  smallest thing that can be non-linear; it must beat the linear IC model or it does not exist.

---

## 1. Pre-registration (to be appended as `preregistration`, name `cascade_intensity`)

### 1.1 The claim, stated so it can die

> **H5.** At minute `t`, a forecast built from the as-of liquidation map has positive
> out-of-sample rank correlation with realized liquidation notional in `(t, t+h]`, **in excess
> of** the correlation achieved by a map-blind forecast built from the tape's own trailing
> liquidation intensity and realized volatility.

Two things must hold; either failing kills H5.

| | must hold | fails if |
|---|---|---|
| **IC** | full model IC_t ≥ 3 over ≥ 30 OOS days | the forecast does not rank forward intensity |
| **map, not echo** | full-minus-blind IC increment t ≥ 3 | the information is self-excitation and vol; the map is decoration |

The second is the only interesting one. Liquidations cluster in time on their own (one forced
sell moves price into the next stop), so a trailing-intensity feature will carry IC with
certainty. H5 is the claim that the *map* adds to it.

### 1.2 The cascade, defined (detection; descriptive, not the target)

For coin `c`, side `s ∈ {long, short}` (long liquidations are forced sells, short liquidations
forced buys), window `w = 5 min`:

- `L_s(t) = Σ px·sz` over realized liquidations of side `s` with `t_event ∈ (t−w, t]`, by
  **event** time (`deploy/liqview.py::liquidations` — never arrival; BTC arrives a median 7 min late).
- `N_s(t)` = distinct `liquidated_user` in the same set.
- `Δ(t)` = log mark return over `(t−w, t]`; `σ_w(t)` = realized std of 5-min returns over the
  trailing 24 h.

A **cascade episode** starts at the first `t` where all four hold, and is debounced 60 min:

1. `L_s(t) ≥ q99` of `L_s` over the trailing 30 days (a per-coin, rolling threshold, never a
   dollar constant — $1M is a cascade on ZEC and a lunch order on BTC);
2. `N_s(t) ≥ 5` — the wallet-count floor that excludes the single-whale bins of §0;
3. `|Δ(t)| ≥ 2 σ_w(t)`;
4. `sign(Δ)` agrees with the side: down for long liquidations, up for short.

This is applied to the tape only to count episodes and to confirm that the continuous target
rises into them. **It is not trained on.** Its count is reported on every card.

### 1.3 The target (prediction)

For horizon `h ∈ {5, 15, 30} min` and side `s`:

```
y_{s,h}(t) = log(1 + L_s over (t, t+h])
```

Three horizons, two sides, per coin. `y` is a rank target; its scale is irrelevant to the IC.
A secondary target `r_h(t)` = signed forward log return is scored for description only and
decides nothing (it is H2's territory).

### 1.4 Features, all strictly as-of `t`

**Map block** (from the as-of snapshot, newest with `t_ingest < t` and age ≤ `MAX_MAP_AGE_NS`,
the rule in `features/liquidations.py`; **null if none, never zero**):

- `m_{s,k}` — visible notional in shell `k` on side `s`, shells `(0, 0.5%], (0.5, 1%], (1, 2%]`
  of mark, the inner three of `labels/touch.py::BANDS`;
- `d_s` — distance from mark to the nearest shell on side `s` holding ≥ `CLUSTER_MIN_NOTIONAL`;
- `imb_1`, `imb_5` — the snapshot's own ±1% and ±5% asymmetry;
- `coverage`, `map_age_s` — carried so the evaluation can be sliced by them.

**Blind block** (tape only, no map):

- `L_s` trailing over 5, 15, 60 min (the self-excitation terms);
- `N_s` trailing 15 min;
- log return over 5, 15, 60 min; `σ` over 60 min and 24 h;
- hour-of-day (UTC) as sin/cos.

**Not in**: funding, OI, order book. Each is a separate registered amendment if added; the
bridge document (`docs/CASCADE_MICROSTRUCTURE_BRIDGE.md`) is where the book enters, later.

### 1.5 Universe and bars

Coins at ≥ 25% registry coverage for the whole evaluation window — today BTC, ETH, SOL. One
row per minute per coin per side. Mark bars are the snapshot marks (~60 s cadence);
a minute with no snapshot within 120 s is **absent**, not interpolated, and the row is dropped.

### 1.6 Model for the IC stage

Ridge regression (`α` chosen by inner CV on the training fold only) on the standardized
feature vector, one model per `(coin, side, h)`, two variants:

- **full**: map block + blind block;
- **blind**: blind block only.

Linear by construction so that an IC here is attributable. Nothing non-linear exists before §1.9.

### 1.7 The statistic

Per UTC day `d` in the OOS set, Spearman `ρ_d` between the fold's OOS forecast and `y` over all
minutes of `d`. Report, per `(coin, side, h)` and per variant:

```
IC      = mean_d ρ_d
IC_t    = IC / (sd_d ρ_d / sqrt(D))          D = number of OOS days
ΔIC     = IC_full − IC_blind,  with its own t over the paired daily differences
```

The paired difference is the claim. One-sided, `t ≥ 3`, no multiple-comparison correction
*within* the registered grid because the grid is fixed here: 3 coins × 2 sides × 3 horizons
= 18 cells, and the decision rule in §1.8 names which must pass.

### 1.8 Controls and decision rule, committed before the result

- **Purged walk-forward, embargo 24 h**, training window ≥ 20 days, test block 7 days
  (`validate.evaluate` as shipped; a change is an amendment).
- **Circular-shift placebo on the map block only**, ≥ 200 replications: map features shifted
  by a random offset ≥ 1 day against the labels and the blind block, which stay fixed. The
  real `ΔIC` must exceed the 99th percentile of the placebo `ΔIC`. This asks exactly: does the
  *timing* of the map's values carry information, or just their distribution?
- **No cost model.** H5 is not a strategy and `Costs` does not apply; a confirmed H5 is an
  input to H2's gate, not a trade.

**PROCEED** (to §1.9) iff, for BTC at `h = 15`, long side **and** short side both show
`ΔIC t ≥ 3` and clear the placebo. Those two cells are the primary; the other 16 are reported
and are secondary.
**REFUTED** iff BTC `h = 15` full-model `IC_t ≥ 3` but `ΔIC t < 1` on both sides over ≥ 45
OOS days — the tape predicts itself and the map adds nothing.
**UNDECIDABLE** otherwise, and the card says so with the days accrued.

### 1.9 The model stage, gated

Opened only on PROCEED. A single-hidden-layer perceptron, ≤ 16 units, tanh, on the same
standardized features, trained on the same purged folds with early stopping on the inner fold
only. It is scored by the identical §1.7 statistic and is **kept only if** its OOS IC exceeds
the ridge's by a paired `t ≥ 2` over the same days. Otherwise the ridge stands and the
perceptron is recorded as tried and refuted. No architecture search: a second architecture is a
second registered entry with its own seq, so that the number of things tried is on the ledger.

### 1.10 Floors

- ≥ 30 OOS UTC days per primary cell, after purging, with a fresh map (≤ `MAX_MAP_AGE_NS`) on
  ≥ 80% of minutes of each counted day. Days below 80% are dropped, not imputed.
- Capture status on 2026-10-04: ~60% snapshot density over the last 87.6 h and parquet
  compaction dead since 2026-08-08 (`FINDINGS.md`). **Repairing capture is the clock**, not the
  market. Earliest honest verdict: 30 clean OOS days after 20 training days, from the day
  capture is clean — not before early December 2026 if fixed this week.

### 1.11 What would refute, and what is merely undecidable

Refuted: §1.8's REFUTED line. Undecidable: anything that fails on floors, coverage, or placebo
count. A model that fits in-sample is nothing; only §1.7 on OOS days counts.

### 1.12 Open items that must be closed before registration

1. **Is `liquidations` venue-wide or observer-limited?** The table is "one row per trade id,
   whoever saw it" with an `observer` column. If events are only those of wallets we track,
   `L_s` is a coverage-biased target and the IC is on a third of the venue. Settle this from
   the capture code and state it in §1.3.
2. **Side of a liquidation.** Confirm `sz` sign or `method` encodes long-vs-short; if not, side
   is inferred from `px` versus `mark_px` and that rule is written here.
3. **Mark source for minute bars** — snapshot marks versus a trade-derived bar; pick one and name it.

---

## 2. What this would mean, either way

Confirmed: the visible map carries timing information about forced flow beyond its echo, which
is the premise H2's accelerator rests on, now measured directly rather than assumed. Refuted:
liquidation intensity is predictable from itself and from vol, which is still useful — a
nowcast of forced flow is a nowcast — but the map is not where the information lives, and the
positions sweep is then a coverage expense without an information return. Either answer closes
a question the heatmap cannot.
