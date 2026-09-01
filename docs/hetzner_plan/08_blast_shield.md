# 08 — Test blast shield (T6)

**Effort** 4 h · **Status** done, 2026-08-29 · **Branch**
`fix/test-blast-shield-and-fake-hl`

## What
Stop the suite writing into the canonical store, and add a local Hyperliquid the
recorded failures can be reproduced against. 730 lines, all under `tests/`,
**zero production diff**.

## How
An autouse fixture redirects every module-level `Path` under `data/` that a
deploy module exposes, and rebinds any function whose signature captured one as
a default. A session backstop reads what was appended during the run and fails
on any record written with a test's clock.

`tests/fake_hl.py` serves the venue on loopback and drives the real `WsClient`,
`InfoClient` and `Capture`.

## Verify
```
uv run pytest -q                      # 677 passed
./test.sh                             # 41 passed, 0 failed
grep -c '"t_ingest":1755000000000000000' data/actions.jsonl   # still 64
```

## Done when
Done. Three spec errors were found by building it — the proposed
`sys.modules` hook would have protected nothing, there are five defaulted
helpers not four, and a size-based backstop false-positives against the live
daemon. See `09` next.
