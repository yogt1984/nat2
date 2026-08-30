"""Resolving the capture universe, and surviving the venue not answering.

The resolve sat outside every handler. `RuntimeError: info metaAndAssetCtxs
failed after 4 attempts` killed the daemon before it opened a single writer,
`Restart=always` turned that into a loop, and the loop wrote no tape at all:
123 such tracebacks across 1,367 unit starts, in 11 bursts -- the worst 55
restarts over 20.8 minutes -- for about 2 h 17 m of capture downtime in ten
days. One episode alone cost 26.8 minutes of `hl.trades`, against a budget of
sixty gap-minutes per week.

Every one of them was local rather than the venue: 82 x `[Errno -3] Temporary
failure in name resolution` and 41 x `All connection attempts failed`, all
within 8-9 s of the start. The venue was never reached, so the HTTP timeout
never even came into play.

Retrying harder is not the fix. Of the 123 failing starts only 10 -- 8.1% --
would have succeeded on the next one, because these outages last minutes, not
seconds. What fixes it is having yesterday's answer: the universe changes when
the venue lists or delists, which is rare, so the previous resolve is almost
always still right and is unconditionally better than not capturing.

So: retry briefly, then fall back to the last universe resolved for *this same
request*, and raise `UniverseUnavailable` only when there is no such record.

**The empty guard is the load-bearing part.** A cached `[]` is not a smaller
version of the same problem, it is a worse one. `Capture` builds its writers
from `streams` regardless of the coin list, but subscribes to nothing, so the
poller keeps appending assetctxs while trades and l2book stay silent until the
300 s stall watch exits -- *without a traceback*. That loop would satisfy this
task's own acceptance criterion while capturing almost nothing. An empty
resolve is therefore treated as a failure and never written.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from nat2.core.clock import now_ns
# Imported as a module, not `from ... import MAX_ATTEMPTS`: the suite's one
# established lever for making a retry test fast is monkeypatching that
# attribute (tests/test_fake_hl.py), and a from-import would bind the value at
# import time and make the lever inert.
from nat2.hl import info as info_module

CACHE = Path("data") / "ops" / "capture_universe.json"
# The same bound gapwatch keeps its incident queue at; this is a record of
# recent starts, not an archive.
KEEP = 20
# The backoff `ws.py` already reconnects with. No new number enters the code.
BACKOFF_S = 1.0
BACKOFF_MAX_S = 30.0


class UniverseUnavailable(RuntimeError):
    """The venue did not answer and no cached universe matches this request.

    Raised rather than guessed at: a wrong coin list is not a smaller failure
    than no coin list, because the tape it produces looks complete.
    """


def cache_path(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / CACHE


def remember(key: dict, coins: list[str], root: Path | None = None) -> None:
    """Append this resolve to the record. One file, but a bounded *list*:
    a single overwritten slot cannot answer which start used which universe,
    which is the question an incident asks."""
    if not coins:                      # see the module docstring
        return
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    records = _load(path)
    records.append({"key": key, "coins": sorted(coins), "n": len(coins), "t_ingest": now_ns()})
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"records": records[-KEEP:]}, separators=(",", ":")))
    os.replace(tmp, path)              # atomic: a torn cache is a poisoned one


def recall(key: dict, root: Path | None = None) -> list[str] | None:
    """The newest universe resolved for exactly this request, or None.

    Exactly, not approximately. A universe resolved under a different
    `min_volume` or a different roster spec is a different question's answer,
    and capturing the wrong coins silently is worse than refusing to start.
    """
    for record in reversed(_load(cache_path(root))):
        if record.get("key") == key and record.get("coins"):
            return list(record["coins"])
    return None


def _load(path: Path) -> list[dict]:
    try:
        blob = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    records = blob.get("records")
    return records if isinstance(records, list) else []


def all_key(min_volume: float, testnet: bool) -> dict:
    return {"mode": "all", "testnet": bool(testnet), "min_volume": float(min_volume)}


def roster_key(spec, testnet: bool) -> dict:
    """Everything that changes which coins come back, and nothing that does not.

    `map_min_coverage` is excluded deliberately: it selects the map universe,
    not the captured roster, so including it would refuse a perfectly usable
    cache after an unrelated coverage change.
    """
    return {
        "mode": "roster", "testnet": bool(testnet),
        "top_n": int(spec.top_n), "min_volume": float(spec.min_volume),
        "pin": sorted(spec.pin), "b_min_volume": float(spec.b_min_volume),
    }


async def resolve(fetch, key: dict, root: Path | None = None,
                  on_event=None) -> tuple[list[str], str]:
    """`(coins, "live" | "cache")`, or raise `UniverseUnavailable`.

    `fetch` is an async callable returning the coin list. The caller owns the
    client it closes over and must close it in a `finally` -- under retry the
    old code leaked one `AsyncClient` per attempt, because `aclose()` sat on
    the line after the call that raised.
    """
    last: Exception | None = None
    backoff = BACKOFF_S
    for attempt in range(info_module.MAX_ATTEMPTS):
        try:
            coins = list(await fetch())
        except Exception as exc:  # noqa: BLE001 - see below; breadth is the point
            # Deliberately every exception, because the daemon's survival must
            # not depend on having enumerated the venue's failure modes
            # correctly. `post()` raises RuntimeError, but at least three other
            # kinds escape the same call: `InfoClient.universe` unpacks
            # `meta, ctxs = await self.meta_and_asset_ctxs()` outside `post`'s
            # own try (ValueError on a short body), indexes `asset["name"]`
            # (KeyError on a changed shape), and a malformed `retry-after`
            # header raises ValueError from inside `post`'s own 429 handler,
            # bypassing its retry loop entirely. Narrowing this tuple would
            # reintroduce exactly the traceback this module exists to remove.
            # CancelledError and KeyboardInterrupt are BaseException, so
            # shutdown still works.
            last = exc
        else:
            if coins:
                remember(key, coins, root)
                return coins, "live"
            last = ValueError("the venue answered with an empty universe")
        if attempt + 1 < info_module.MAX_ATTEMPTS:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)

    cached = recall(key, root)
    if cached:
        if on_event:
            on_event(f"universe unavailable ({type(last).__name__}); "
                     f"falling back to {len(cached)} cached coin(s)")
        return cached, "cache"
    raise UniverseUnavailable(
        f"could not resolve the universe and no cached one matches this request: {last}"
    )
