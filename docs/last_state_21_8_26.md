# Last state — 2026-08-21

Snapshot of where the reduced-scope programme (`TASK_2/REDUCED_SPECS.md` v0.3, tasks 00–09) stands, what is running, what is waiting on the clock, and what is waiting on the operator. Code lives in `~/nat2` (`main` = `origin/main` after this push) and `~/nat`; plans and pre-registrations live here in `~/TASK_2`. Ledger facts are as of `~/nat2/data/ledger.jsonl` seq 134.

## 1. Task board

| # | Task | State |
|---|------|-------|
| 00 | nat2 capture/cycle under systemd | ✅ 08-20 |
| 01 | Manifest gap watchdog + ntfy | ✅ 08-20 |
| 02 | nat gap-alert + log rotation | ✅ 08-20 (Telegram creds still empty) |
| 03 | Pre-register map thresholds + magnet N | ✅ seq 117–119 |
| 04 | Cluster-level `gate map` scoring | 🟡 code done (`d245f31`); verdict when 1000 forward events — ~08-27 |
| 05 | Event-timeline logger | ✅ capturing since 08-20 15:18 |
| 06 | Static status page | ✅ merged `bf5abec`; Hetzner caddy deploy open |
| 07 | Position reconstruction from tape | ✅ merged `93b65ac`; exact on gap-free capture (seq 132) |
| 08 | `gate magnet` | 🟡 wrapper merged `e742141`; refusing on record (seq 134); verdict ~09-12 |
| 09 | α-kernel expert (criterion 2) | 📝 pre-registration doc written, **awaiting review** — nothing on the ledger, no code |
| — | gapwatch intra-file heartbeat (07 follow-up) | ✅ merged `ffd426c`, induced ntfy alert received |

## 2. What is running (dev box, user systemd)

`nat2-capture.service`, `nat2-cycle.service`, `nat2-gapwatch.timer` (5 min), `nat2-evlog.timer` (5 min), `nat2-statuspage.timer` (10 min → `~/www/status/status.html`); nat: `nat-ingestor.service`, `nat-logrotate.timer`, `nat-candle-refresh.timer`. Caddy is **not** a service here; it was validated locally and stopped.

## 3. Gate ladder (`nat2 gate status`, 2026-08-21 morning)

| gate | verdict | note |
|------|---------|------|
| feed | FAIL (20 h old) | `hl.trades:no_gaps`, `hl.l2book:no_gaps` — the 08-20 18:21–18:54 CEST capture hole. Re-run after a clean day; downstream is void until it passes. |
| map | refused | `insufficient_forward_events`; forward window opened 08-20 14:31 (seq 119). |
| magnet | refused | `upstream_map` (seq 134, commit `e47f8cc`, `clean_tree=false`). |
| persistence, decay | never run | not in the reduced scope |

## 4. Findings of 08-20/21 worth remembering

1. **Tape reconstruction is exact** when capture is continuous: 3,307 `userFills` block checkpoints across 8 wallets, `exact_frac = 1.0` (seq 132). `userFills` `tid` order ≠ execution order inside a block; `startPosition` chains only across blocks; a capped (2000) response may truncate its oldest block; after a WS reconnect the first records carry a backlog of older prints, so gap detection must use `t_ingest`. All encoded in `core/reconstruct.py` and its tests.
2. **33-min capture hole** 08-20 18:21–18:54 CEST inside a normally rotated file; invisible to manifest-based gapwatch → fixed by the open-file mtime heartbeat (5 min). It also failed `gate feed`.
3. **`gate magnet` cannot PASS as built**: HYPOTHESIS_1 §6 criterion 2 (α kernel) has no implementation in `MagnetA`. Operator chose option (b): a dedicated α-kernel expert under its own pre-registration (task 09).
4. **Persisted map snapshots are band-cumulative only** (4 bands/side, cross *imbalance* only, no buckets, no liquidity scaling) — task 09's definition is the shell form on those fields so the 08-13 clock is kept.
5. Registry `positions_ts()` moves with every derived replay; the sweep epoch is `published_ts()` (added in 07).

## 5. Waiting on the operator

1. **Review `TASKS/09_alpha_kernel_expert.md` §1** → reply "register" (with edits if any) → `nat2 log add` → code per §2. Must land before the window fills (~09-12) or the α choice is made after seeing data.
2. **`without_map`/`map_only` WIP** (08-13, unreviewed) in `src/nat2/experts/magnet_a.py` + `src/nat2/features/spec.py`: commit, stash, or discard. Until then every gate entry records `clean_tree=false`, and task 09 must branch from a clean tree.
3. Hetzner caddy deploy of the status page (host access).
4. Small: Telegram creds (02); real `nat2-evlog.timer` disable test (05); CPI receipt-lag measurement on 09-11 (05).

## 6. Dates

- ~08-27: `gate map` forward window fills (1000 events) → first map verdict.
- 09-11 08:30 ET: CPI release — evlog receipt-lag measurement.
- ~09-12: 2000 events ∧ 30 days → `gate magnet` runnable (needs map PASS and task 09 registered + built).

## 7. Commits pushed today (`~/nat2` main → origin/main)

`57dcfb2` `9aac647` `fec4c25` `93b65ac` (task 07) · `e47f8cc` `e742141` (task 08) · `6999248` `ffd426c` (gapwatch heartbeat) · this document under `docs/`.
