# 03 — The ASCII heatmap

**Effort** 2 h · **Depends on** 02 · **Status** todo

## What
The picture: time across, price up, liquidation notional as intensity, with the
mark path drawn over it.

## How
One column per snapshot, downsampled to terminal width (`shutil.get_terminal_size`,
default 100). Downsampling **sums** notional within a column bin and takes the
*last* mark — a mean mark would invent a price the venue never printed.

Intensity ramp, densest last: ` .:-=+*#%@`. Scale is **logarithmic** — cluster
notional spans four orders of magnitude on real data (a live BTC row: 233k to
2.8M in adjacent buckets, and far more across the span), so a linear ramp shows
one hot cell and nothing else. Normalise per render, and print the value that
maps to `@` in the legend, so two runs are never silently on different scales.

The mark path overprints the ramp as `·`, or as a distinct colour when the
output is a TTY and `--no-colour` is absent. Colour is optional decoration; the
glyph must carry the information on its own, because this gets piped into
`less` and pasted into notes.

Header, always: coin, window, view (02), `bucket_pct`, `span`, **coverage**,
`published_frac`, row count, and the `@` scale value. Footer: the legend.

Rows default to 40 and are `--rows` adjustable; a bucket is 0.25% of mark, so
40 rows over a ±30% span is a genuine downsample and the header says so.

## Verify
```
python3 deploy/liqview.py --coin BTC --since 6h
```
Renders inside the terminal width with no wrapping. A known synthetic input
produces a byte-exact expected frame (golden test, task 08). `--rows 1` and
`--width 20` do not crash. A window with no data prints "no snapshots in
window" and exits 1 — never an empty grid, which reads as "no clusters".

## Done when
A real 6-hour BTC window renders legibly at 80, 100 and 200 columns, and the
golden frame matches byte for byte.
