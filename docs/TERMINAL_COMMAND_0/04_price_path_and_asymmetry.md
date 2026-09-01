# 04 — The price path and the asymmetry strip

**Effort** 1.5 h · **Depends on** 03 · **Status** todo

## What
Two things the heatmap alone cannot show: where price actually went, and
whether the map was lopsided *before* it went there.

## How
**The price path.** `mark` per snapshot, overprinted on the heatmap and also
drawn as its own sparkline row so it survives a dense heatmap. Sampled at the
snapshot cadence (60 s). A finer path from `hl.trades` is a `--trades` opt-in,
never the default — it needs an as-of join, and a join is where lookahead gets
in.

**The asymmetry strip.** One character row per band, under the heatmap, showing
`imb` over time. `imb` is already on every snapshot row, per band
(0.5/1/2/5/10/20/30%), so nothing is derived here — it is read and drawn.

Glyph ramp for imbalance, signed: `▼▽·△▲` for strongly-down / down / balanced /
up / strongly-up, or ASCII `vV·^A` under `--ascii`. Thresholds for the glyph are
**presentation only** and must be printed in the legend; they decide nothing and
so need no pre-registration. Say so in a comment, or the next reader will
reasonably assume they are a signal.

The point of the strip is temporal ordering: the eye should be able to check
whether the lopsidedness preceded the move or followed it. That is the whole
difference between a magnet and a coincidence, and it is exactly what `gate
magnet` tests formally under a pre-registration.

## Verify
```
python3 deploy/liqview.py --coin BTC --since 12h --bands 0.01,0.02,0.05
```
The strip has one row per requested band, aligned column-for-column with the
heatmap above it. On the synthetic ramp from 02, the strip shows sustained
one-sided imbalance in the columns *before* the mark reaches the cluster.

## Done when
Strip and heatmap share an x axis exactly, and `--trades` changes only the path
row, never the heatmap.
