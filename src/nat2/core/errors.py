"""Error attribution.

A failure count is not a diagnosis. Capture recorded 2,298 poll errors and a
sweep recorded 2,177 failures, and neither could say *why* -- the exception was
incremented into a counter and discarded. One reason repeated 2,177 times is a
single bug; 2,177 different reasons is a different problem entirely, and the
counter cannot tell them apart.

`reason` collapses an exception to a short stable key so failures aggregate
into something answerable.
"""

from __future__ import annotations

import re
from collections import Counter

STATUS = re.compile(r"\b(\d{3})\b")


def reason(exc: BaseException) -> str:
    """A short, stable key for an exception: type plus HTTP status if present."""
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"{name} {status}"
    # RuntimeError("info X failed after 4 attempts: Client error '429 ...'")
    match = STATUS.search(str(exc))
    if match and name == "RuntimeError":
        return f"{name} {match.group(1)}"
    return name


def top_reasons(counter: Counter, limit: int = 3) -> str:
    if not counter:
        return ""
    return ", ".join(f"{key} x{count}" for key, count in counter.most_common(limit))
