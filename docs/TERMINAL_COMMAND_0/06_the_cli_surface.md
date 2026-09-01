# 06 — The CLI surface

**Effort** 1 h · **Depends on** 03, 04, 05 · **Status** todo

## What
One command, memorable enough to reach for under pressure, with flags that
cannot silently change what the picture means.

## How
`python3 deploy/liqview.py` — a deploy script, not a `nat2` subcommand, for the
same reason `tapecheck` is: it must run when the venv does not.

```
--coin BTC              required; one coin per frame
--since 6h              window back from now (also --until for a fixed window)
--view absolute|relative        default absolute (task 02)
--rows 40 --width auto  grid size; auto from the terminal
--bands 0.01,0.02,0.05  which imbalance rows to draw
--liquidations          overlay realized events (task 05)
--trades                finer price path, opt-in (task 04)
--ascii                 no box-drawing, no colour; for pipes and notes
--json                  the underlying slice, no rendering
--watch 60              redraw every N seconds
```

`--json` matters more than it looks: it is the seam that keeps this honest. The
renderer consumes exactly what `--json` emits, so anything on screen can be
recovered as numbers and checked, and nothing can be drawn that is not in the
data.

`--watch` redraws on a timer. It must not accumulate memory across redraws and
must exit cleanly on SIGINT — this is the mode that gets left running for hours.

Exit codes: `0` rendered, `1` no data in window, `2` bad arguments. Never `0`
with an empty grid; an empty picture reads as "no clusters", which is a
different and much stronger claim than "no data".

## Verify
```
python3 deploy/liqview.py --coin BTC --since 6h
python3 deploy/liqview.py --coin BTC --since 6h --ascii | cat      # no escapes
python3 deploy/liqview.py --coin NOPE --since 6h; echo $?          # 1, with a reason
python3 deploy/liqview.py --coin BTC --since 6h --json | python3 -m json.tool >/dev/null
```

## Done when
All four behave as stated, `--ascii | cat` contains no escape sequences, and
`--json` round-trips through `json.tool`.
