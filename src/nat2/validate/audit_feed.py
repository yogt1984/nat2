"""Feed integrity audit -- the check behind `gate feed`.

Everything downstream trusts three properties of the store, so all three are
tested here rather than assumed:

  intact    closed files still hash to their manifest digest
  complete  per-stream sequence numbers have no holes
  causal    t_ingest never runs backwards, and never precedes t_event

The third is the one that matters most.  If our clock sits behind the
exchange's, `t_ingest` stops being an upper bound on what we could have known,
and every feature built on the store inherits a lookahead we cannot see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nat2.core.clock import NS, now_ns
from nat2.hl.ratelimit import STALE_AFTER_DAYS, VERIFIED_ON, table_age_days
from nat2.io.worm import SUFFIX, read_manifest, read_records, sha256_file

# A negative ingest lag is impossible if the clocks agree, but NTP jitter and
# HL's own timestamping make a small negative tail unremarkable.  Beyond this,
# the causality guarantee is not credible.
MAX_CLOCK_SKEW_NS = 2 * NS
# A run of missing records longer than this many times a stream's typical
# cadence is a hole, not jitter.
GAP_TOLERANCE = 60.0
STALE_TOLERANCE = 20.0


@dataclass
class Check:
    name: str
    stream: str
    passed: bool
    detail: str
    stats: dict = field(default_factory=dict)


@dataclass
class AuditResult:
    checks: list[Check]
    window_ns: int

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    def summary(self) -> dict:
        return {
            "window_ns": self.window_ns,
            "checks": len(self.checks),
            "failed": [f"{c.stream}:{c.name}" for c in self.failures],
            "stats": {f"{c.stream}:{c.name}": c.stats for c in self.checks if c.stats},
        }


def _percentile(values: list[int], q: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def audit(root: Path, streams: list[str], window_ns: int) -> AuditResult:
    root = Path(root)
    since = now_ns() - window_ns
    checks: list[Check] = [_check_limit_table()]
    checks += _check_manifest(root, since)
    for stream in streams:
        checks += _check_stream(root, stream, since)
    return AuditResult(checks, window_ns)


def _check_limit_table() -> Check:
    age = table_age_days()
    ok = age <= STALE_AFTER_DAYS
    return Check(
        "limits_verified",
        "-",
        ok,
        f"rate-limit table last verified {VERIFIED_ON} ({age}d ago)"
        + ("" if ok else " -- re-check against HL docs and update VERIFIED_ON"),
        {"age_days": age},
    )


def _check_manifest(root: Path, since: int) -> list[Check]:
    entries = [e for e in read_manifest(root) if e.last_ingest >= since]
    bad = []
    missing = []
    for entry in entries:
        path = root / entry.path
        if not path.exists():
            missing.append(entry.path)
            continue
        digest, size = sha256_file(path)
        if digest != entry.sha256 or size != entry.bytes:
            bad.append(entry.path)
    checks = [
        Check(
            "manifest_intact",
            "-",
            not bad and not missing,
            f"{len(entries)} closed files verified"
            + (f"; MODIFIED: {bad}" if bad else "")
            + (f"; MISSING: {missing}" if missing else ""),
            {"files": len(entries), "modified": len(bad), "missing": len(missing)},
        )
    ]

    known = {e.path for e in read_manifest(root)}
    on_disk = {
        str(p.relative_to(root))
        for p in root.rglob(f"*{SUFFIX}")
        if p.stat().st_mtime * NS >= since
    }
    # The current hour's file is legitimately open and unmanifested; anything
    # older means a writer died without closing, so its tail may be truncated.
    orphans = sorted(on_disk - known)
    stale_orphans = [
        p for p in orphans if (root / p).stat().st_mtime < (now_ns() - 2 * 3600 * NS) / NS
    ]
    checks.append(
        Check(
            "no_orphan_files",
            "-",
            not stale_orphans,
            f"{len(orphans)} open/unmanifested file(s)"
            + (f"; UNCLOSED FROM A DEAD WRITER: {stale_orphans}" if stale_orphans else ""),
            {"orphans": len(orphans), "stale": len(stale_orphans)},
        )
    )
    return checks


def _check_stream(root: Path, stream: str, since: int) -> list[Check]:
    from nat2.hl.schemas import STREAMS

    spec = STREAMS.get(stream)
    cadence = spec.cadence_hint_s if spec else 10.0
    has_clock = spec.has_event_clock if spec else False

    seqs: list[int] = []
    ingests: list[int] = []
    lags: list[int] = []
    unreadable = ""
    try:
        for rec in read_records(root, stream, since_ns=since):
            seqs.append(rec["seq"])
            ingests.append(rec["t_ingest"])
            if rec.get("t_event") is not None:
                lags.append(rec["t_ingest"] - rec["t_event"])
    except Exception as exc:  # noqa: BLE001 - corruption is a verdict, not a crash
        # A store we cannot read is a FAIL, never a traceback: the gate exists
        # to give downstream commands an answer, and "it exploded" is not one.
        unreadable = f"{type(exc).__name__}: {exc}"

    if unreadable:
        return [
            Check(
                "readable",
                stream,
                False,
                f"decode failed after {len(seqs)} record(s) -- {unreadable}",
                {"records_before_failure": len(seqs)},
            )
        ]

    if not seqs:
        return [Check("has_data", stream, False, "no records in window", {"records": 0})]

    checks = [Check("has_data", stream, True, f"{len(seqs)} records", {"records": len(seqs)})]

    holes = [
        (a, b) for a, b in zip(seqs, seqs[1:]) if b != a + 1 and b > a
    ]
    checks.append(
        Check(
            "seq_continuous",
            stream,
            not holes,
            "no sequence holes" if not holes else f"{len(holes)} hole(s), e.g. {holes[:3]}",
            {"holes": len(holes), "lost": sum(b - a - 1 for a, b in holes)},
        )
    )

    backwards = sum(1 for a, b in zip(ingests, ingests[1:]) if b < a)
    checks.append(
        Check(
            "ingest_monotonic",
            stream,
            backwards == 0,
            "t_ingest non-decreasing"
            if backwards == 0
            else f"{backwards} backwards step(s) -- our clock moved back",
            {"backwards": backwards},
        )
    )

    if has_clock and lags:
        skew = min(lags)
        checks.append(
            Check(
                "clock_causal",
                stream,
                skew >= -MAX_CLOCK_SKEW_NS,
                f"ingest lag median {_percentile(lags, 0.5) / 1e6:.0f}ms "
                f"p99 {_percentile(lags, 0.99) / 1e6:.0f}ms min {skew / 1e6:.0f}ms"
                + ("" if skew >= -MAX_CLOCK_SKEW_NS else " -- t_ingest PRECEDES t_event"),
                {
                    "median_ms": _percentile(lags, 0.5) / 1e6,
                    "p99_ms": _percentile(lags, 0.99) / 1e6,
                    "min_ms": skew / 1e6,
                },
            )
        )

    gaps = [b - a for a, b in zip(ingests, ingests[1:])]
    worst = max(gaps) if gaps else 0
    limit = cadence * GAP_TOLERANCE * NS
    checks.append(
        Check(
            "no_gaps",
            stream,
            worst <= limit,
            f"largest gap {worst / NS:.1f}s (cadence ~{cadence:.0f}s, limit {limit / NS:.0f}s)",
            {"worst_gap_s": worst / NS, "limit_s": limit / NS},
        )
    )

    age = now_ns() - ingests[-1]
    stale_limit = cadence * STALE_TOLERANCE * NS
    checks.append(
        Check(
            "fresh",
            stream,
            age <= stale_limit,
            f"last record {age / NS:.1f}s ago (limit {stale_limit / NS:.0f}s)",
            {"age_s": age / NS},
        )
    )
    return checks
