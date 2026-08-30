"""The capture universe, and what happens when the venue will not answer.

The resolve used to sit outside every handler: 123 `RuntimeError: info
metaAndAssetCtxs failed after 4 attempts` tracebacks across 1,367 unit starts,
in 11 bursts, the worst 55 restarts over 20.8 minutes. The daemon died before
opening a single writer, so `Restart=always` produced a loop that captured
nothing -- one episode cost 26.8 minutes of `hl.trades` against a budget of
sixty gap-minutes per week.

Every failure was local (`[Errno -3]`, `All connection attempts failed`) rather
than the venue, and only 8.1% of them would have succeeded on the next start.
So the fix is not a longer retry, it is the previous answer.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from nat2.hl import info as info_module
from nat2.io.universe import (
    UniverseUnavailable,
    all_key,
    cache_path,
    recall,
    remember,
    resolve,
)

COINS = ["BTC", "ETH", "SOL"]


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    # The suite's established lever (tests/test_fake_hl.py): two attempts, not
    # four. universe.py reads it off the module for exactly this reason.
    monkeypatch.setattr(info_module, "MAX_ATTEMPTS", 2)


def _run(coro):
    return asyncio.run(coro)


async def _dead():
    raise RuntimeError("info metaAndAssetCtxs failed after 4 attempts: [Errno -3]")


# --- the headline -----------------------------------------------------------

def test_a_dead_venue_with_a_matching_cache_still_starts(tmp_path, monkeypatch):
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    key = all_key(min_volume=5e6, testnet=False)
    remember(key, COINS, root=tmp_path)

    coins, provenance = _run(resolve(_dead, key, root=tmp_path))
    assert coins == COINS and provenance == "cache"


def test_a_dead_venue_with_no_cache_raises_rather_than_guessing(tmp_path, monkeypatch):
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    with pytest.raises(UniverseUnavailable):
        _run(resolve(_dead, all_key(5e6, False), root=tmp_path))


def test_a_cache_for_a_different_request_is_not_used(tmp_path, monkeypatch):
    # A universe resolved under a different min_volume answers a different
    # question. Capturing the wrong coins silently is worse than not starting.
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    remember(all_key(min_volume=5e6, testnet=False), COINS, root=tmp_path)
    with pytest.raises(UniverseUnavailable):
        _run(resolve(_dead, all_key(min_volume=1e6, testnet=False), root=tmp_path))
    with pytest.raises(UniverseUnavailable):
        _run(resolve(_dead, all_key(min_volume=5e6, testnet=True), root=tmp_path))


# --- the guard that matters most --------------------------------------------

def test_an_empty_resolve_is_never_cached_and_never_returned(tmp_path, monkeypatch):
    """A cached `[]` is the worst outcome available, not the smallest.

    Capture builds its writers from `streams` regardless of the coin list but
    subscribes to nothing, so the poller keeps appending assetctxs while trades
    and l2book stay silent until the 300 s stall watch exits -- with no
    traceback. That loop would satisfy this task's own acceptance criterion
    while capturing almost nothing.
    """
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    key = all_key(5e6, False)

    async def empty():
        return []

    with pytest.raises(UniverseUnavailable):
        _run(resolve(empty, key, root=tmp_path))
    assert recall(key, root=tmp_path) is None
    remember(key, [], root=tmp_path)
    assert recall(key, root=tmp_path) is None


def test_an_empty_resolve_falls_back_to_a_good_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    key = all_key(5e6, False)
    remember(key, COINS, root=tmp_path)

    async def empty():
        return []

    coins, provenance = _run(resolve(empty, key, root=tmp_path))
    assert coins == COINS and provenance == "cache"


# --- provenance -------------------------------------------------------------

def test_the_record_is_a_bounded_list_not_one_overwritten_slot(tmp_path):
    # One slot cannot answer which start used which universe, which is the
    # question an incident asks.
    key = all_key(5e6, False)
    for n in range(25):
        remember(key, COINS[: 1 + n % 3], root=tmp_path)
    records = json.loads(cache_path(tmp_path).read_text())["records"]
    assert len(records) == 20                       # KEEP, as gapwatch bounds its queue
    assert all("t_ingest" in r and "key" in r for r in records)
    assert recall(key, root=tmp_path) == sorted(COINS[:1 + 24 % 3])


def test_a_live_resolve_is_remembered_and_reported_as_live(tmp_path):
    key = all_key(5e6, False)

    async def good():
        return COINS

    coins, provenance = _run(resolve(good, key, root=tmp_path))
    assert provenance == "live" and coins == COINS
    assert recall(key, root=tmp_path) == sorted(COINS)


def test_the_resolve_retries_before_giving_up(tmp_path, monkeypatch):
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    calls = []

    async def flaky():
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("All connection attempts failed")
        return COINS

    coins, provenance = _run(resolve(flaky, all_key(5e6, False), root=tmp_path))
    assert provenance == "live" and coins == COINS and len(calls) == 2


def test_a_short_body_raises_valueerror_and_is_still_handled(tmp_path, monkeypatch):
    # `InfoClient.universe` unpacks `meta, ctxs = await ...` outside `post`'s
    # own try, so a short body escapes as ValueError rather than RuntimeError.
    monkeypatch.setattr("nat2.io.universe.BACKOFF_S", 0.0)
    key = all_key(5e6, False)
    remember(key, COINS, root=tmp_path)

    async def short_body():
        meta, ctxs = [{"universe": []}]             # noqa: F841 - the real unpack
        return []

    coins, provenance = _run(resolve(short_body, key, root=tmp_path))
    assert provenance == "cache"


def test_a_torn_cache_is_treated_as_no_cache(tmp_path):
    key = all_key(5e6, False)
    path = cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\x00\x00 not json")
    assert recall(key, root=tmp_path) is None
