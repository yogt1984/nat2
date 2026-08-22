"""Compare two tapes of the same venue, hour by hour, from their manifests alone.

TASK_2/12 runs a second capture on a second host. Whether the two tapes agree is
the acceptance check for the primary, and it is answered without decompressing
anything: the manifest records, per closed part, the stream, the hour it covers
and the number of records it holds. Two honest captures of the same websocket
see the same prints, so equal counts per hour is the expectation and any
difference names the hour to look at.

Comparison only. Merging parts across tapes is deliberately not here: a foreign
part carries its own sequence numbers, so it would fail `seq_continuous` in the
feed audit unless entries were origin-tagged -- a design decision for the audit,
not a side effect of a comparison tool.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from nat2.io.worm import read_manifest


def hours(root: Path, stream: str | None = None) -> dict[str, dict[str, int]]:
    """{stream: {hour_key: records}} over the closed parts of a tape."""
    out: dict[str, dict[str, int]] = {}
    for entry in read_manifest(root, stream):
        # <stream>-<hour>-<part>.ndjson.zst; the hour key is the second-to-last dash field.
        hour = Path(entry.path).name.split(".ndjson", 1)[0].rsplit("-", 2)[-2]
        by_hour = out.setdefault(entry.stream, {})
        by_hour[hour] = by_hour.get(hour, 0) + entry.lines
    return out


@dataclass
class Comparison:
    stream: str
    same: int = 0
    differ: list[tuple[str, int, int]] = field(default_factory=list)   # (hour, ours, theirs)
    only_ours: list[str] = field(default_factory=list)
    only_theirs: list[str] = field(default_factory=list)

    @property
    def overlapping(self) -> int:
        return self.same + len(self.differ)

    def summary(self) -> dict:
        return {"stream": self.stream, "overlapping_hours": self.overlapping, "same": self.same,
                "differ": self.differ, "only_ours": self.only_ours, "only_theirs": self.only_theirs}


def compare(ours: Path, theirs: Path, tolerance: float = 0.0) -> list[Comparison]:
    """Hour-by-hour record counts; an overlapping hour differs when the relative gap exceeds `tolerance`."""
    a, b = hours(ours), hours(theirs)
    out = []
    for stream in sorted(set(a) | set(b)):
        c = Comparison(stream)
        ha, hb = a.get(stream, {}), b.get(stream, {})
        for hour in sorted(set(ha) | set(hb)):
            if hour not in hb:
                c.only_ours.append(hour)
            elif hour not in ha:
                c.only_theirs.append(hour)
            elif abs(ha[hour] - hb[hour]) <= tolerance * max(ha[hour], hb[hour]):
                c.same += 1
            else:
                c.differ.append((hour, ha[hour], hb[hour]))
        out.append(c)
    return out
