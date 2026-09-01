# 08 — Tests and verification

**Effort** 2 h · **Depends on** 01–07 · **Status** todo

## What
The evidence that the picture is the data, and that a wrong picture fails
loudly rather than looking plausible.

## How
`tests/test_liqview.py`, loaded by path like `tests/test_gapwatch.py` and
`tests/test_tapecheck.py` — `deploy/` is outside the package on purpose.

**Synthetic store fixture.** A scratch WORM root with a known map: one cluster
pinned at an absolute price, a mark that ramps past it, and a handful of
realized liquidations. Everything downstream is asserted against a picture
whose right answer is known by construction.

**The tests that matter:**

1. *Absolute vs relative* — the ramp fixture yields a horizontal cluster in one
   view and a diagonal in the other (task 02). This is the test that catches
   the error most likely to produce a false magnet.
2. *Golden frame* — a fixed window renders byte-exactly. Catches silent changes
   to the ramp, scale or layout.
3. *Log scale* — buckets spanning four orders of magnitude produce more than
   one distinct glyph. A linear scale fails this, which is the point.
4. *No data is not no clusters* — an empty window exits 1 with a reason, never
   0 with an empty grid.
5. *Torn manifests* — both shapes (NUL-padded, and truncated with a good entry
   appended) are tolerated, matching `read_manifest`.
6. *Liquidation counts* match a direct SQL count, and `t_event` is used rather
   than `t_ingest` — assert with an event deliberately given a two-hour ingest
   lag, which must draw at its event time.
7. *Read-only* — no `open(...,'w')`, no `mkdir`, no `unlink` anywhere in
   `deploy/liqview.py`; the registry is opened `mode=ro`. Assert by grep, the
   way task 13 proved the same property.
8. *Width and rows* — 20, 80, 200 columns and 1, 40 rows all render without
   wrapping or crashing.

**Real-data smoke, before commit:** a 6-hour BTC frame from the live store, and
`--json` cross-checked against the venv's `read_records` for one snapshot.

## Verify
```
uv run pytest tests/test_liqview.py -q
./test.sh
python3 deploy/liqview.py --coin BTC --since 6h        # by eye, on real data
```

## Done when
The suite is green, the golden frame matches, the read-only grep is clean, and
a real 6-hour frame renders legibly at three terminal widths.
