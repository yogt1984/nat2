# 02 — Two views, and why the absolute one is the point

**Effort** 30 min · **Depends on** 01 · **Blocks** 03 · **Status** todo

## What
The buckets are stored **relative to mark** (`lo_pct`, price recoverable as
`mark * (1 + lo_pct)`). Mark moves between snapshots. So there are two ways to
draw the same data, and they answer different questions.

## How
Implement both; default to absolute.

**Mark-relative** — y axis is % from mark; price is a straight line down the
middle and clusters drift as price moves. Good for reading the *shape* of the
map and its asymmetry at a glance. Cheap: no regridding.

**Absolute price** — y axis is price. Clusters sit still where they actually
are and the mark path wanders across them. This is the view that can show price
travelling toward a cluster, which is the whole hypothesis. It needs each
snapshot's relative buckets regridded onto one fixed price axis.

Regridding rule: the axis spans `[min(mark)*(1-span), max(mark)*(1+span)]` over
the window, divided into `rows` bins; each bucket adds its notional to the bin
holding `mark * (1 + lo_pct)`. **Do not interpolate** — a bucket is an
observation at a price, and smearing it invents structure that was never
observed.

The trap worth stating plainly: with mark moving, a *stationary* cluster in
absolute space is a *drifting* one in relative space, and the reverse. Reading
the relative view as though it were absolute is the easiest way to see a magnet
that is not there. The header always names the view on screen.

## Verify
A synthetic store with a cluster pinned at one absolute price and a mark that
ramps past it:
- absolute view: the cluster is a straight horizontal line the mark path crosses;
- relative view: the cluster is a diagonal converging on zero.

Both rendered from the same input, asserted in tests.

## Done when
Both views render from one slice, the header names the view, and the synthetic
ramp produces exactly those two shapes.
