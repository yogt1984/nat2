# 11 — Capture startup retry (T1)

**Effort** 4 h · **Status** done, 2026-08-30 · **Branch** `feat/capture-universe-retry`

## What
The universe resolve sat outside every handler. Measured over the unit's whole
journal: **123 `RuntimeError: info metaAndAssetCtxs failed after 4 attempts`
tracebacks across 1,367 unit starts**, in 11 bursts — the worst 55 restarts over
20.8 minutes — for about 2 h 17 m of capture downtime in ten days. The daemon
died before opening a single writer, so `Restart=always` produced a loop that
wrote no tape at all. One episode on 2026-08-29 cost **26.8 minutes of
`hl.trades`**, against a budget of sixty gap-minutes per week.

Every failure was local, not the venue: 82 × `[Errno -3] Temporary failure in
name resolution`, 41 × `All connection attempts failed`, all within 8–9 s of the
start. The venue was never reached, so the 30 s HTTP timeout never applied.

## How
`src/nat2/io/universe.py`: retry with the backoff `ws.py` already uses, for
`MAX_ATTEMPTS` read off `info.py` at call time — no new number — then fall back
to the last universe resolved for *this same request*, and raise
`UniverseUnavailable` only when there is no such record. `cli.py` gains a
handler beside the `CaptureStalled` one, so it exits 1 without a traceback.

Retrying harder was never the answer: of the 123 failing starts only **10 (8.1%)**
would have succeeded on the next one. These outages last minutes. What fixes it
is having yesterday's answer.

## Verify
```
uv run pytest tests/test_universe.py -q      # 10 passed
```
Venue unreachable + matching cache → resolves from cache, the daemon starts.
Venue unreachable + no cache → `UniverseUnavailable`, exit 1, no traceback.
Both verified end-to-end through `cli._resolve_universe` against a closed port.

## Done when
Done. Five things building it found:

- **The stated done-when — "24 h of journal contains zero tracebacks" — is not
  a test of this task** and was unmeasurable when written: of 1,316 tracebacks
  over 1,367 starts, only 123 are this bug. The other 1,193 were the torn
  manifest tail (fixed separately, `cbd5892`). Restated: **zero
  `info metaAndAssetCtxs failed` tracebacks in 24 h**, and the honest headline
  metric is the *restart rate* — 1,253 starts on 2026-08-29 against 1–19 on a
  normal day.
- **The cache buys a running daemon against a resolve blip, not against a dead
  venue.** With the venue truly down, capture starts from cache, subscribes to
  nothing reachable, and stands down after the 300 s stall watch. That is still
  the right behaviour — it captures whatever is reachable and exits cleanly —
  but the spec's "the daemon **runs**" overstates it, and both branches of its
  Verify end in exit 1, so that test cannot distinguish them.
- **An empty resolve is worse than the spec says.** `Capture` builds writers
  from `streams` regardless of the coin list but subscribes to nothing, so the
  poller keeps appending assetctxs while trades and l2book stay silent until the
  stall watch exits — *with no traceback*. A poisoned cache would therefore
  satisfy this task's own acceptance criterion while capturing almost nothing.
  Guarded on both write and read.
- **`_live_roster` never used its `info` argument** (AST-verified), which is the
  entire root of the spec's UnboundLocalError trap. Dropping the parameter makes
  the trap unexpressible rather than merely avoided. Note it fires on 100% of
  `--roster` starts and 0% of `--all` starts, and production runs `--all` — a
  smoke test of the deployed unit would never have caught it.
- **`Verdict` was used at three sites in `cli.py` and never imported**, so
  `_live_roster` raises `NameError` on any box whose ledger holds no `map`
  verdict — i.e. a fresh one, which is exactly what task 06 builds. Fixed here
  because the new resolve path calls it.

Also fixed: `await info.aclose()` was unreachable on the failure path, leaking
one `AsyncClient` per attempt under retry. The client is now closed in a
`finally`. See `12` next.
