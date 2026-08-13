# ASKING — reading the liquidation map

What to ask, what it runs, and what the answer means. `nat2 help` already lists every command;
this is the shorter question of *which one to reach for* and *how the output lies to you if you
read it carelessly*.

Scope: seeing clusters. Realized liquidations, capture, gates and the ledger are elsewhere.

All output below is real, captured against Hyperliquid mainnet on **2026-08-13**. Marks, volume
and volatility have moved since; the structure has not. Numbers are dated for the same reason
`FINDINGS.md` dates its measurements — a stale reading presented as current is the failure this
whole system is built to refuse.

---

## "Is what I'm about to look at still true?"

```bash
nat2 wallets status
```

```
data home: /home/onat/nat2 (project root)
wallets: 3601 ({'both': 399, 'equity': 1601, 'volume': 1601})
last snapshot #2: 6883 positions from 1291/3601 wallets, 0 error(s)
positions age: 290.1m
```

**Ask this first, every time.** A liquidation map is a photograph of a book that changes
continuously, and `nat2 map` will draw one from any snapshot you happen to have — it performs no
freshness check at all. Only `gate map` does, and its limit is **6 hours**.

290 minutes is 4.8h: inside the limit, and close enough to it that the next reading should be
preceded by a sweep. A map built from a 22-hour-old snapshot describes positions that have since
been closed, moved or liquidated, and nothing in the output will tell you so.

Read `1291/3601` as the real story: only a third of the registry held anything. That is normal and
it is why the registry is seeded by equity — it wants whoever holds size, not whoever exists.

---

## "Where are BTC's liquidation clusters right now?"

Three commands, in order.

```bash
nat2 wallets status                                   # is it stale?
nat2 wallets snapshot                                 # if so — ~6 min, whole weight budget
nat2 map BTC --span 40 --resolution 1 --buckets 14
```

```
snapshot 2: 6883 positions from 1291/3601 wallets, 0 error(s), 361s
```

```
│ 66095.2 │  +3.50% │  $134.3M │   ██████████████████████ │   99% │
│ 65456.6 │  +2.50% │   $26.8M │                     ████ │   14% │
│   64818 │  +1.50% │  $145.2M │ ████████████████████████ │   99% │
│ 64179.4 │  +0.50% │   $98.6M │         ████████████████ │  100% │
│    mark │         │          │                  63860.1 │       │
│ 62902.2 │  -1.50% │   $31.6M │                    █████ │   43% │
│ 62263.6 │  -2.50% │    $8.4M │                        █ │   72% │
│   61625 │  -3.50% │  $119.4M │      ███████████████████ │   17% │
│ 60986.4 │  -4.50% │   $20.7M │                      ███ │    8% │
│ 59709.2 │  -6.50% │    $3.7M │                        █ │   96% │
│ 59070.6 │  -7.50% │   $30.0M │                     ████ │   97% │
│ 57793.4 │  -9.50% │   $68.1M │              ███████████ │   99% │
```

Columns:

| | |
|---|---|
| `%` | distance from mark. **This is the number that matters**, not the price. |
| `notional` | dollars force-sold if price reaches that bucket. |
| `cross` | fraction on cross margin — see below, it changes what the bar *is*. |

`cross` is not decoration. An **isolated** position is a hard point mass at a fixed, known price. A
**cross** position's liquidation level *moves* whenever anything else in that account moves, so it
is a smear that drifts toward the mark as the account bleeds elsewhere. The +1.50% stack above is
99% cross — soft. The −3.50% stack is 17% cross — mostly hard, fixed levels. Equal-looking bars,
different objects.

The three flags are not cosmetic, which is the next question.

---

## "Am I seeing the whole map?"

Usually not. Run the same command with defaults and read the footer:

```bash
nat2 map BTC
```

```
coverage 35.9% of venue position notional (OI x2) · 549 positions
(97% published, 50 unplaceable, 363 beyond +/-10%)
60 more bucket(s) not shown -- raise --buckets; the bars are scaled to the
largest bucket displayed, not the largest one
```

**363 positions were off-screen and 60 buckets were suppressed.** The default `--span 10` is a
window, and a window that hides its own edges invites you to mistake it for the whole picture.
Worse, the bars rescale to the largest bucket *displayed* — so truncating does not merely omit
rows, it silently redraws every remaining bar against a false maximum.

```bash
nat2 map BTC --span 40 --resolution 1 --buckets 14
```

| flag | what it does | why it matters |
|---|---|---|
| `--span` | how far from the mark to look, percent | the default hid the largest clusters |
| `--buckets` | rows displayed per side | truncation rescales every bar |
| `--resolution` | bucket width, percent | **display only** — band totals and imbalance come from exact liquidation prices and do not change |

Always read the two footer warnings before drawing a conclusion. If either fires, widen and look
again.

---

## "Which cluster actually matters?"

The map shows where mass is. It ranks nothing.

```bash
nat2 map BTC --span 40 --resolution 1 --rank
```

```
    BTC — which cluster is worth pushing into
┃ side ┃   psi ┃ conc ┃     d* ┃    mass ┃  n ┃
│   up │ 0.501 │ 100% │ +0.43% │  $45.1M │  1 │
│ down │ 0.161 │  29% │ -3.19% │ $137.1M │ 48 │

sigma 0.99%/day (measured, 169 candles), volume $1,816M/day,
cost 0.10% round trip, kappa 0.5, omega_cross 0.5
psi > 1 means a push into that side pays for itself. Unfitted: A = 1 and the
cost is asserted, so the level is not yet a claim -- the ordering is.
```

**The tallest bar is not the answer.** BTC's biggest cluster is $145.2M at +1.50%. The ranked
answer is **$45.1M at +0.43%** — three times smaller, three times closer, and `psi` 0.501 against
0.161.

That inversion is the point of the ratio, and it is not a heuristic. Walk cost grows *linearly* in
distance while a cascade's displacement grows as the *square root* of mass, so proximity
systematically beats size. You cannot eyeball it off a histogram, which is why the panel exists.
The derivation is in [`ATTACK.md`](ATTACK.md).

`psi > 1` means a push into that side pays for itself. Nothing here is fitted — `A = 1` and the
cost is asserted — so trust the **ordering**, not the level. The panel says so itself on every run.

---

## "Is that a cluster, or one whale?"

The `conc` column, and it is the one most worth internalising.

```
│   up │ 0.501 │ 100% │ +0.43% │  $45.1M │  1 │
```

100% concentration, `n = 1`. **That entire reading is a single wallet.** A supremum is brittle by
construction — one mispriced large position sets it — so the ratio is recomputed with the winning
cluster's largest member removed, and `conc = 1 − psi_jackknife/psi` reports how much collapsed.

Compare the downside: `psi` is lower at 0.161, but `conc` is 29% across 48 positions. Structurally
that is a real crowd; it simply is not currently worth attacking. Without this column the first row
reads as a setup.

`nat2` warns on its own when a reading is this fragile:

```
100% of a reading rests on one position -- a supremum is brittle, and this one
is a single wallet
```

---

## "How much of the book am I even seeing?"

```
coverage 35.9% of venue position notional (OI x2) · 549 positions
(97% published, 50 unplaceable, 363 beyond +/-40%)
```

**Coverage qualifies everything above it.** 35.9% means the other ~64% of venue positioning holds
clusters you cannot see. `gate map` fails below **25%**, so this book clears the floor — a partial
sweep (`--limit`) will not, and will drop coverage with no error at all.

Three caveats, in descending order of how much they could hurt:

- **`OI x2` is unverified.** HL's `openInterest` counts each contract once, so venue-wide position
  notional is taken as twice OI notional. If that convention is wrong, coverage is wrong by a
  factor of two — and coverage is the single number the map is judged by. It is on the
  verify-before-coding list in `TASKS.md`.
- **Coverage by notional is not coverage by events.** The registry is seeded by equity, so it sees
  whoever holds size. The wallets that actually get liquidated skew small and leveraged.
- **`97% published`** is good news: that fraction uses HL's own `liquidationPx` rather than our
  derivation, which reproduces the published value for only ~44% of positions.

`50 unplaceable` are positions we observe — they count toward coverage — but whose liquidation
price could not be established, so they cannot be drawn.

---

## "Why did the number change?"

Two reasons, and both are worth knowing before you trust a shift.

**Volatility is measured, and asserting it will fool you.** `--rank` pulls hourly candles and
computes realized daily vol. Asserting σ = 2%/day put BTC's upside at `psi = 0.990` — sitting
right on the actionable threshold. Measured from 169 candles it is **0.99%/day**, and `psi` is
**0.501**. The guess had doubled it. The panel always states which it used:

```
sigma 0.99%/day (measured, 169 candles)     ← measured from HL candles
sigma 2.00%/day (asserted)                  ← you passed --sigma
```

**Staleness.** Between two readings the snapshot may simply have aged. `wallets status` before and
after tells you whether the book moved or your photograph did.

---

## What it refuses to do, and why

The refusals are the product. Each of these is a case where an answer existed and was withheld
because it would have been wrong.

| you get | it means |
|---|---|
| `registry is empty -- run \`nat2 wallets seed\` first` | no wallets. Earlier this returned an empty registry *as fact* when run from another directory. |
| `NOTACOIN is not a listed perp` | the universe is rebuilt from `meta` every run, never hardcoded. |
| `no registry positions in BTC -- snapshot first` | the registry exists but holds nothing for this coin. |
| `cannot rank: HL returned N hourly candle(s), too few to measure volatility` | σ sets the displacement a cascade produces, so a fabricated one quietly sets the answer. Pass `--sigma` to assert one deliberately. |
| `cannot rank: no 24h volume for this coin` | volume is the impact denominator. Without it there is no ratio. |
| `fuel on both sides -- a volatility state, not a direction` | both sides are viable. That is not a small net signal; it is a state to stand aside from. |

---

## The five ways to get a plausible wrong answer

| trap | tell | fix |
|---|---|---|
| stale snapshot | nothing — `map` does not check | `nat2 wallets status` first; 6h limit |
| truncated view | `N more bucket(s) not shown` | raise `--span` and `--buckets` |
| rescaled bars | same warning, easy to skim past | bars scale to the largest bucket *shown* |
| one-wallet cluster | `conc` near 100% | read `conc` and `n` before `psi` |
| asserted volatility | `(asserted)` in the footer | let it measure, or know what you asserted |

---

## What it costs

A full sweep is **~6 minutes and the entire IP weight budget** — 3,601 wallets, one
`clearinghouseState` request each, and HL limits per IP rather than per process. It is not a thing
to run in a loop, and a sweep running concurrently with capture will starve it.

Snapshot on a schedule (`nat2 cycle --snapshot-every 6h`), then read the map as often as you like.
Reading is cheap; the photograph is not.
