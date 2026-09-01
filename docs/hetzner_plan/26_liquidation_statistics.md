# 26 — Liquidation statistics (T11)

**Effort** 1.5 days · **Depends on** 25 · **Status** todo

## What
The census has 11,035 rows over ten months and nothing renders it. Invisible
today: 6,843 episodes, a 470-fill maximum, 14 coin-minutes above $1M, a 5.37%
backfill tail, and 25 event-days over 299 calendar days.

The one liquidation chart that exists is recomputed over the whole table every
pass — **so it can only rise, and reading it as a trend is reading an
artefact**.

## How
Nine descriptive blocks: daily series with a census band and tape-hole overlay;
hour-of-day; per-fill **and** per-episode distributions; the cascade table;
per-coin with side sources; the ingest-lag CDF; census quality; map-as-of context
with set-asides named; and the population series as **two populations, never a
line**.

**No gate, no threshold, no hit rate** — the page must not be able to contaminate
the forward window.

Two blockers before extracting the chart helpers: the status page has no
`import sys`, and `svg_lines` calls a module-local helper that will not travel.

## Verify
Every number reproduces against a sha256-pinned registry copy, and the existing
golden passes **unregenerated**.

## Done when
The page renders hourly, golden untouched.
