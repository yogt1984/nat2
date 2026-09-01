# 21 — Testing agents (T12)

**Effort** 2–3 days · **Depends on** 10 · **Status** todo

## What
The second opinion. The watchdog cannot see the failures that happened: over
08-20…08-28 capture had **112 invocations, 66 tracebacks and 71 non-zero exits,
but only 1 left a same-hour seam** in the manifest. Seventy crash starts are
structurally invisible to a manifest-only watchdog.

## How
Nine agents plus a shared `common.py`, following the existing `deploy/evlog/`
layout, on the system interpreter so a broken venv cannot blind the auditor.

Four rules encoded **in code, not documentation**: a default-deny CLI allowlist,
with a test asserting its three sets partition every leaf, so a new command fails
the build until classified; an off-host run never files an incident; an
agent-chosen threshold cannot file one; and every staleness threshold must exceed
**twice** the cadence it observes.

Consider `--no-file` for week 1: every incident is an unlocked ledger append
racing the cycle daemon until 09 lands.

## Verify
Replayed against fixtures built from the real manifest and journal, it reports
the four historical failures with their measured values.

## Done when
One pass writes a line per finding, exits 0, and no agent writes outside
`tmp_path`.
