# 05 — Realized liquidations overlay

**Effort** 1.5 h · **Depends on** 03 · **Status** todo

## What
The map shows where liquidations *would* happen. This marks where they *did*,
so the two can be compared in one frame.

## How
Read `registry.liquidations` — 12,596 events, 141 coins, 6,930 wallets, back to
2024-08-11 — filtered to the coin and window. Columns used: `t_event`, `px`,
`sz`, `method`, `liquidated_user`. Open the database **read-only**
(`file:...?mode=ro`), because a live daemon owns it and this tool renders only.

Marks overprint the heatmap at `(t_event, px)`. Size sets the glyph: `x` small,
`X` large, with the split printed in the legend as presentation, not policy.

**Three honesty requirements**, each of which changes what the frame means:

1. **Use `t_event`, never `t_ingest`.** Task 27 measured **593 of 11,035
   liquidations (5.37%) arriving more than two hours late**. Drawn by arrival,
   a cascade appears after the move that caused it, and the picture invents
   causality backwards.
2. **Late arrivals must be visible.** An event whose `t_ingest - t_event`
   exceeds the window is drawn with a distinct glyph and counted in the footer.
   A frame rendered live and the same frame rendered tomorrow will differ; the
   footer is what tells you why.
3. **Say what is missing.** `liqscan` reads `userFills`, capped at 2,000 fills
   per wallet, so events scroll out of reach permanently. The footer prints the
   observed event count and the plain sentence that this is observed
   liquidations, not all liquidations.

Cascades are **not** detected here. Grouping events into cascade objects is
task 26 of the migration plan and needs a pre-registered definition; drawing
them would smuggle that definition in through a renderer.

## Verify
```
python3 deploy/liqview.py --coin BTC --since 24h --liquidations
```
Marks appear at plausible prices relative to the mark path. Cross-check the
count against:
```
sqlite3 'file:data/registry.sqlite?mode=ro' \
  "SELECT COUNT(*) FROM liquidations WHERE coin='BTC' AND t_event > ...;"
```
The two counts match exactly.

## Done when
Counts match the database, the registry is never opened for writing, and a
late-arriving event is visibly distinct from an on-time one.
