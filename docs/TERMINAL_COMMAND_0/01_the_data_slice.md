# 01 — The data slice

**Effort** 1 h · **Depends on** 00 · **Status** todo

## What
Read one coin's `nat2.liqmap2` snapshots over a time window, stdlib-only, from
a store that a live daemon is writing to.

## How
`deploy/liqview.py`, modelled on `deploy/tapecheck.py`: `ROOT` from
`Path(__file__).resolve().parent.parent`, no import from `nat2.*`, and its own
manifest reader.

The reader must tolerate what the store actually contains, because both shapes
exist on the live box today:

- a **crash-torn manifest line** — NUL-filled blocks from an unclean shutdown;
- a **disk-full torn line** — truncated with no trailing newline, with the next
  start's good entry appended onto it.

`src/nat2/io/worm.py` handles both (skip the first, salvage the second). This
file re-implements the same rule rather than importing it, for the same reason
`tapecheck` does: a broken venv is one of the states you want to look at the
tape from. **Copy the rule, and say in a comment that it is a copy.**

Selection: parts whose `[first_ingest, last_ingest]` overlaps the window, then
records filtered by `t_ingest`. Decompress with `zstandard` if importable, else
shell out to `zstd -dc` — note `zstandard` is a venv package, so the fallback
is the path that survives the venv being gone.

Each snapshot yields, per coin: `t_ingest`, `mark`, `coverage`,
`published_frac`, `buckets` (`[lo_pct, notional, cross_notional, positions]`),
`imb`, `near`.

## Verify
```
python3 deploy/liqview.py --coin BTC --since 2h --json | head -40
```
Row count matches the snapshot cadence: 60 s ⇒ ~120 rows in 2 h. Every row has
a `mark` and a non-empty `buckets`. Cross-check one row against
`read_records` from the venv:
```
.venv/bin/python -c "from nat2.io.worm import read_records; ..."   # same mark, same bucket count
```

## Done when
The stdlib reader and the venv reader return byte-identical marks and bucket
counts for the same window, and the reader survives a manifest with both torn
shapes injected into a scratch copy.
