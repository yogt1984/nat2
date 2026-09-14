# MASTER_PLAN — one namespace, four tiers

Status: **active**. Written 2026-09-14. Supersedes the sequencing in `TASKS.md`,
`docs/last_state_21_8_26.md` and the ordering (not the content) of
`docs/hetzner_plan/README.md`.

This file exists because the work was spread across five numbering schemes in which the same
numbers meant different things, so nothing could be sequenced. It assigns one id namespace, fixes
the order, and records for every item what it is, how it is verified, and what it unblocks.

It does not restate mechanism. Where a task already has a specification, the row points at it and
that document remains authoritative for *how*. This file is authoritative only for *which, in what
order, and done when*.

---

## 0. The one rule this plan is built on

Clean tape-days are the only resource that cannot be bought back. Code, specification and
refactoring are cheap and recoverable; a day of holed capture is gone. Every ordering decision
below follows from that, and from one measurement:

> Since 2026-09-10 the capture journal holds **1,394 RuntimeError, 292 ConnectionClosedError,
> 198 OSError and 143 TimeoutError**, against exactly **one** clean `RuntimeMaxSec` recycle.
> 2026-09-12 has no parts at all. A healthy day holds ~29 parts; 09-10 holds 3, 09-11 holds 2.

Capture is not degrading, it is crash-looping, and the defect is in the reconnect path rather than
in the host. That single fact reorders the programme: the migration is deferred, because moving a
crash-looping daemon produces a crash-looping daemon on a new box a month later.

Inherited rules, unchanged and binding on every row below:

| rule | source |
|---|---|
| Gates before models; a downstream command refuses when its upstream gate is missing, FAIL or REFUSED | `README.md` |
| Exact or nothing; every map carries its coverage number | `README.md`, `ASKING.md` |
| Pre-registration precedes scoring code; no new threshold in code without a ledger entry first | `README.md`, `HYPOTHESIS_1..4` |
| Amendments are registered, never silently applied; extensions are new entries with their own floors | `HYPOTHESIS_1..4` |
| Below any floor the result is undecidable, never negative | `HYPOTHESIS_1..4` |
| Nothing sends an order; action-log L3 stays empty until a gate PASSes | `README.md`, `DESIGN.md` |
| Task branch, conventional commit, `merge --no-ff`; never on `main` | `docs/hetzner_plan/README.md` |
| Planted test before real data; real-data smoke before commit | `docs/hetzner_plan/README.md` |

---

## 1. Id namespace

Ids are `P01`…`Pnn`, flat and permanent. **An id is never renumbered and never reused**, so a row
may move between tiers without changing its name. Tier is a column, not part of the id.

Status vocabulary is the one already in use: `todo · doing · done · blocked · deferred`.

`Ledger?` means the item requires a ledger entry *before* the code or the measurement it governs.
`Unblocks` names the gate or task that cannot proceed until this row is done.

### Crosswalk from the old schemes

The five schemes stay readable as history. Nothing is renumbered inside the old files; this table
is the only bridge. A row absent here is either complete or moved to §7.

| old id | old scheme | new id |
|---|---|---|
| `hetzner_plan` 00 | in-repo, infrastructure | P12 (the decision half only) |
| `hetzner_plan` 01–07, 15–18 | in-repo, infrastructure | P20 (conditional, collapsed) |
| `hetzner_plan` 08–13, 19 | in-repo, code | complete; see §7 |
| `hetzner_plan` 14 | in-repo, code | P21 (acceptance half only) |
| `hetzner_plan` 20, 22 | in-repo, acceptance | P22 (conditional) |
| `hetzner_plan` 21 | in-repo, tooling | P23 (conditional) |
| `hetzner_plan` 23, 24 | sibling `nat` repo | out of scope; see §7 |
| `hetzner_plan` 25 | decision | P13 |
| `hetzner_plan` 26, 27 | in-repo, measurement | P24, P25 |
| `TERMINAL_COMMAND_0` 03, 04, 06, 08 | in-repo, liqview | P19 |
| `TERMINAL_COMMAND_0` 07 | in-repo, liqview remote | P19 (deferred half) |
| `TASKS.md` 1, 1b, 1c | milestone-keyed | superseded; content absorbed into P10, P14, P15 |
| `TASKS.md` 2, 3, 4 | milestone-keyed | P16, P17, P26 |
| `~/TASK_2` 00–09 | outside repo | historical; not re-imported |
| `DESIGN.md` M0–M6 | milestone | retained as milestone labels only, not as task ids |

---

## 2. Tier 0 — restore the clock and the namespace

Nothing else in this plan is worth starting before this tier closes. Two of the four rows are
paperwork that takes an hour and removes a class of wrong decisions.

### P01 · Adopt one namespace and one status document
**Status** todo · **Ledger?** yes (one `plan_adopted` entry) · **Unblocks** every row below

What: this file becomes the only place work is sequenced. `README.md` remains the only statement of
current system status.

How: append a ledger entry recording the adoption and the crosswalk hash. Move `TASKS.md` and
`docs/last_state_21_8_26.md` to `docs/archive/` with their dates in the filename, and add a
one-line header to each saying which rows of this file absorbed them. Do not edit their bodies;
they are history.

How verified: `grep -rl "next step\|TODO" *.md docs/*.md` returns no document outside this one that
claims to sequence work.

Done when: the ledger entry exists, `docs/archive/` holds both files, and `README.md`'s status
block is the only live status statement.

### P02 · Fix the capture crash loop
**Status** todo · **Ledger?** no · **Unblocks** P03, and through it every gate

What: capture exits non-zero on network churn instead of reconnecting. The error mix since
2026-09-10 is 1,394 RuntimeError, 292 ConnectionClosedError, 198 OSError, 143 TimeoutError against
one clean recycle. `FINDINGS.md` already carries the unexplained 49% `metaAndAssetCtxs` failure
rate after 19.5 h as an open cause; this is the same wound, now fatal rather than degrading.

How: reproduce against `tests/fake_hl.py` (task 08's loopback venue) by dropping the socket mid
stream and asserting the daemon reconnects rather than exits. Fix the reconnect path. Treat
`RuntimeMaxSec=5h` as the recycle it is, not as the crash handler it has become.

How verified: a planted test that kills the connection N times and asserts zero process exits;
then 72 h of real capture.

Done when: 72 consecutive hours with at least one part per clock hour, zero `exit-code` failures in
`journalctl --user -u nat2-capture.service`, and `gapwatch` reporting no open hole.

### P03 · Take `gate feed` to a verdict on the repaired tape
**Status** blocked on P02 · **Ledger?** no (the gate writes its own) · **Unblocks** P10, P11

What: feed is the gate that kills everything, and it is currently FAIL at ledger seq 2385.

How: `nat2 gate feed` over a window that lies entirely inside the clean run from P02.

Done when: a PASS verdict for feed is in the ledger over a window of at least 24 h, and the accrual
date it prints is recomputed rather than copied from any document.

### P04 · Defer the migration on evidence, not on preference
**Status** todo · **Ledger?** yes (one `decision` entry) · **Unblocks** P20, P21, P22, P23

What: `docs/hetzner_plan` is 28 tasks of production operations (restic, Caddy, DNS, dead-man,
blast shield) wrapped around a research tape, of which 20 are untouched. If the defect found in P02
is in nat2's code, it travels to any new host and the migration buys nothing this quarter.

How: after P02 has held for 14 days, write the decision entry. Either capture is stable on su-35,
in which case the migration is deferred and P20–P23 stay `deferred`; or a named failure mode
requires a different host, in which case P20 is scoped to a **capture-only** subset (a box that
stays up, a disk that does not fill, a watchdog) and the remaining infrastructure tasks stay
deferred.

Done when: the ledger holds a decision entry naming which branch was taken and the evidence for it.

---

## 3. Tier 1 — settle what can void data you are still accruing

Every row here is hours of work and can make thirty days of tape worthless. All six are
pre-registration matters, so they must land **before** more accrual, not after. That ordering is
not a preference; it is the standing rule.

### P05 · Settle `OI_SIDES`
**Status** todo · **Ledger?** yes (amendment) · **Unblocks** P06, P10, P11, accelerator

What: the convention `OI_SIDES = 2` is assumed and unverified against Hyperliquid's documentation.
It is a factor of two on coverage, and coverage is the single number `gate map` and the accelerator
pre-registration both threshold on at 25%.

How: verify against HL's published documentation and against one cross-check computed from the
venue's own figures. Register the answer.

Done when: a ledger amendment fixes the convention with its source, and `TASKS.md`'s
verify-before-coding entry for it is closed in `docs/archive/`.

### P06 · Fix one authoritative coverage number
**Status** blocked on P05 · **Ledger?** yes · **Unblocks** P10, P11

What: three figures circulate. `DESIGN.md` says 69% of venue OI; `CASCADE_MICROSTRUCTURE_BRIDGE.md`
citing `FINDINGS.md` after a 2x denominator correction says BTC ~30%, ETH ~36%, SOL ~27%;
`ASKING.md` shows BTC 35.9%. The corrected figure is the later statement and should win, but it must
be recomputed under P05's answer rather than quoted.

How: recompute per coin from the current registry and the P05 convention. Register the result.
Amend `DESIGN.md`'s capture section, and mark the conclusion it drew from 69% as superseded.

Done when: one number per coin exists in the ledger, every document quoting a coverage figure
either cites it or is archived, and `DESIGN.md` no longer asserts 69%.

### P07 · Decide which band defines the imbalance
**Status** todo · **Ledger?** yes (amendment) · **Unblocks** P10, P11

What: `FINDINGS.md` measured the imbalance sign flipping between bands, -0.206 at 5% and +0.272 at
10%, and $573M of BTC mass sitting beyond +/-5% against $112.6M inside it. `ATTACK.md` and
`CONSISTENCY.md` both treat the imbalance baseline as one well-defined object. It is not. As
written, the mandated baseline has no band-independent direction and the pre-registered cells are
not comparable to each other.

How: either pre-register the single band that defines direction, with the reason, or declare the
cells explicitly band-specific and register the band as part of each cell's identity. The `liqmap2`
stream at +/-30% is the instrument that can answer which; it has been accruing since 2026-08-22.

Done when: a registered amendment states the band rule, and no cell in `HYPOTHESIS_1.md` can be
scored without it being determined.

### P08 · Register `tapecheck_v1`
**Status** todo · **Ledger?** yes · **Unblocks** closes a debt under P02

What: `hetzner_plan` task 10 says plainly that this pre-registration is the one thing still owed,
and task 19, merged later, already depends on the hole floor it would fix. Two merged tasks rest on
a ledger entry that nothing records as created. The two tasks also disagree on behaviour when it is
absent: 10 refuses, 19 deliberately continues so the only alarm is not blinded.

How: settle the hole floor (the open decision in `hetzner_plan` 00), register it, and record the
asymmetry between 10 and 19 as deliberate in the entry so it stops reading as an inconsistency.

Done when: the entry exists and `deploy/tapecheck.py` no longer refuses.

### P09 · Restore or retire the fifth baseline
**Status** todo · **Ledger?** yes (amendment) · **Unblocks** P11

What: `ATTACK.md` §8 mandates five comparators (null, imbalance baseline, gravity / free-alpha,
placebo, momentum) and states that only beating all five is the hypothesis. `CONSISTENCY.md` §7
lists four and omits the gravity arm, then §8 says "against all four". The gravity arm is the one
that falsifies the square-root impact law, so dropping it silently weakens the test the whole
magnet branch rests on.

How: decide which is correct and register it. If the arm is retired, the entry must say why, and
`ATTACK.md` §8 must be amended rather than left contradicting the registration.

Done when: one count of baselines exists across both documents and the ledger.

### P10 · Reconcile the build state of Psi
**Status** todo · **Ledger?** no · **Unblocks** nothing; removes a false signal

What: `ATTACK.md` declares the attack ratio specified and implemented in `features/attack.py` with
tests. `CONSISTENCY.md` §11 lists "Psi, impact-scaled mass — not built". The code exists;
`CONSISTENCY`'s table is the older statement.

How: correct `CONSISTENCY.md` §11 and its overall status line. No code change.

Done when: no document claims Psi is unbuilt.

---

## 4. Tier 2 — force one verdict

The project has four hypotheses and no verdicts. One completed falsification cycle is worth more
than a fifth hypothesis, because it is the only thing that tells you whether the map branch
deserves the next six months.

### P11 · Take `gate map` to a verdict
**Status** blocked on P03, P05, P06 · **Ledger?** no · **Unblocks** P12

What: map is FAIL at ledger seq 2355. It must be rerun under the settled coverage convention, not
the one it last ran under.

Done when: a PASS or FAIL verdict for map is in the ledger, computed on a window entirely inside
the clean tape, at the registered coverage floor. A REFUSED on insufficient forward events is not a
verdict and leaves this row open.

### P12 · Take `gate magnet` to a verdict
**Status** blocked on P11, P07, P09 · **Ledger?** no · **Unblocks** P14

What: magnet is REFUSED at ledger seq 2387. It is the concrete form of `HYPOTHESIS_1.md`, and it
needs 2,000 forward scoreable events and at least 30 distinct days per cell.

Note before running: `HYPOTHESIS_1.md` §8 and §9 are stale. They state there is no capture daemon
and no store, and list the purged walk-forward, calibration and hash-stamped `costs.toml` as not
built. All of it exists and is used by `HYPOTHESIS_2.md`. Its readiness section must not be used to
scope this work; its decision rule, written before the data, stands unchanged.

Done when: a CONFIRMED, REFUTED or UNDECIDABLE verdict per cell is in the ledger. Undecidable is
filed as undecidable and never as refuted.

### P13 · Record the consequence and re-scope
**Status** blocked on P12 · **Ledger?** yes · **Unblocks** all of Tier 3

What: the verdict decides what the rest of the programme is for. This row is the point at which
Tier 3 is ordered, not before.

How: append a decision entry stating what the verdict was and which Tier 3 rows it promotes, kills
or leaves deferred. If magnet is refuted, `README.md` already names the next primary question,
whether wallets position correctly before information events, and the event timeline has been
capturing since 2026-08-20 for exactly this case.

Done when: every Tier 3 row has a status that is no longer `deferred by default`.

---

## 5. Tier 3 — conditional on the verdict

Every row here is `deferred` until P13 promotes it. They are listed so the plan is complete, not so
they can be started.

| id | title | source | note |
|---|---|---|---|
| P14 | Evaluate `gate accelerator` against HYPOTHESIS_2 | H2, seq 191/192 | Built, nothing evaluated. Earliest verdict ~2026-11-07. Decide whether it supersedes or sits beside magnet, and amend `DESIGN.md`'s five-gate table either way. |
| P15 | Build the CONSISTENCY estimator | `CONSISTENCY.md` | The largest single block of unbuilt work: fit the two-barrier race classifier and invert to drift, one shared gamma across the 18 cells. Do not regress returns. |
| P16 | Register HYPOTHESIS_3 (hunter) | H3 | Draft, unregistered, and carries `task: unassigned`. Its baseline is defined by H1's outcome, so it cannot be finally scored before P12. |
| P17 | Register HYPOTHESIS_4 (prophet) and build `gate persistence` | H4, `DESIGN.md` M3 | Draft, unregistered, `task: unassigned`. No `gates/persistence.py` exists. |
| P18 | Build `gate decay` | `DESIGN.md` M4 | No code exists. Kills copyability if it fails. |
| P19 | Finish `liqview` | `TERMINAL_COMMAND_0` 03, 04, 06, 08; 07 deferred | Shipped script lacks `--trades`, `--watch`, `--width auto`, `--no-colour`; carries `--span` and `--budget` that no task file mentions. Task 07 (remote) is deferred with the migration. |
| P20 | Capture-only host migration | `hetzner_plan` 01–07, 15–18 | Only if P04 chose to migrate, and then scoped to a box that stays up, a disk that does not fill, and a watchdog. |
| P21 | Backup acceptance | `hetzner_plan` 14, 15 | Code merged 2026-09-01; acceptance blocked on a restic repo that does not exist. |
| P22 | Clean-day scorer and the acceptance count | `hetzner_plan` 20, 22 | Cannot start today: two of the eight clean-day criteria depend on P21 and on the gates timer. The timer is now installed and ran on 2026-09-14; task 20's note that it was never installed is stale. |
| P23 | Testing agents | `hetzner_plan` 21 | No harness in the tree. |
| P24 | Liquidation statistics page | `hetzner_plan` 26 | Measurement, not infrastructure. |
| P25 | `hl.ops` stream and the clock fix | `hetzner_plan` 27 | No `hl.ops` stream on disk. Relevant to the sign-flipping clock skew in §6. |
| P26 | Cascade microstructure bridge | `docs/CASCADE_...md` | Phased plan with kill criteria, cross-repo onto `nat`. Requires its own pre-registration in Phase 0, and two values (`MAX_MICRO_AGE_NS` and the null-rate threshold) that the document requires be fixed but never names. |

---

## 6. Carried open questions

These are unowned by any row above because none of them blocks the critical path, and each is
recorded so it is not rediscovered. Items already promoted to Tier 1 are not repeated here.

- Should builder-deployed perps stay excluded? `DESIGN.md` excludes them, but they dominate the
  cascade event count (BRENTOIL 309, CBRS 91 against BTC 190, ETH 181) and hold the largest
  observed cascade at $13.2M. `FINDINGS.md` explicitly reopens this.
- Why does the liquidation-price derivation reproduce HL's published value exactly for only 48.7%
  of positions on the correct `crossMarginSummary` variant? Prime suspect is size-tiered
  maintenance margin on large positions. Not blocking: the published value is the source of truth.
- Why does clock skew flip sign between runs (-196 ms then +395 ms median ingest lag)? The negative
  case voids the `t_ingest` guarantee that every causal feature depends on. Related to P25.
- What produced the unattributed write into the WORM store at 2026-08-09 12:53:54? Nothing that ran
  accounts for it.
- Is the liquidated population genuinely different from the mapped population, or did we look too
  late? The 0.0% mapped reading is confounded because a liquidated wallet no longer holds the
  position. Needs the snapshot-then-observe cycle, not more analysis.
- Two staleness rules are live: 6 h for reading a map by hand (`gate map` freshness) and 5 minutes
  for scoring. They are different clocks and no document says so.
- Whether the research design is affordable at the sample sizes this tape can produce: floors of
  2,000 non-overlapping observations and 30 distinct days across 18 cells, against roughly 25
  usable tape-days accrued in the first month. Worth deciding deliberately rather than discovering
  in February.

---

## 7. Closed, and deliberately out of scope

**Complete and merged** (`hetzner_plan`, verified against git rather than status headers, which are
stale in both in-repo task sets):

| task | title | evidence |
|---|---|---|
| 08 | Blast shield | merge `0cf3f37`; 730 lines under `tests/`, zero production diff |
| 09 | Ledger lock and WAL | merge `448326b`; 13 adversarial findings all refuted |
| 10 | Tapecheck | merge `f8d21f1`; pre-registration still owed, see P08 |
| 11 | Capture startup retry | merge `dc24301` |
| 12 | Capture shared budget | commit `ec683e0` |
| 13 | Capture disk full | commit `a4c96c9`; leaves an untracked cross-branch defect in tapecheck's cause vocabulary, folded into P08 |
| 19 | Gapwatch honesty | merge `696d751`; fixed an uptime counter under-reporting outages 29x |

`TERMINAL_COMMAND_0` 00, 01, 02 and 05 are complete on the same basis; 03, 04, 06 and 08 are
partial and carried as P19.

**Out of scope for this repo:** `hetzner_plan` 23 (nat ops interpreter) and 24 (nat Rust
reconnect) target the sibling `nat` repository. 24 calls itself the one unaudited spec. They belong
to that repo's plan, not this one.

**Not re-imported:** the `~/TASK_2` 00–09 scheme. It is the history of how the programme got here
and stays readable where it is.

**Superseded documents:** `TASKS.md` and `docs/last_state_21_8_26.md` move to `docs/archive/` under
P01. `docs/BUILD_PROMPT.md` stays where it is but must not be used as a work order: it carries no
date and no status, and following it literally would direct a fresh agent to rebuild M0–M2 over an
existing implementation.

---

## 8. What this plan will look like when it has worked

One of two things is true within the quarter, and both are acceptable outcomes.

Either `gate magnet` returns CONFIRMED on at least one cell, in which case the map branch has
earned the estimator work in P15 and the accelerator evaluation in P14, and the programme has its
first model frozen to `models/` with a card stating what would refute it.

Or it returns REFUTED or UNDECIDABLE, in which case that is the result, it is recorded exactly as
carefully as a positive one would be, and the next primary question named in `README.md` becomes
the programme. The event timeline has been capturing since 2026-08-20 precisely so that this branch
costs nothing to take.

What is not acceptable is a fifth hypothesis before the first one speaks.
