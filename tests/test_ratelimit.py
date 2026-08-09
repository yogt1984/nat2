"""The weight budget must be one account, not one per process.

HL limits by IP. An in-memory budget lets two commands run back to back and
collectively blow through the limit -- which is exactly how `nat2 gate map`
earned a 429 immediately after `nat2 liq scan`.
"""

from __future__ import annotations

import time

import httpx
import pytest

from nat2.hl.info import THROTTLED_BACKOFF_S, InfoClient
from nat2.hl.ratelimit import (
    DEFAULT_INFO_WEIGHT,
    INFO_WEIGHT,
    MIN_SHARE_FRACTION,
    SharedWeightBudget,
    WeightBudget,
    table_age_days,
    weight_of,
)


def test_weight_table_has_a_default_for_unknown_endpoints():
    assert weight_of("clearinghouseState") == INFO_WEIGHT["clearinghouseState"]
    assert weight_of("someEndpointHLAddedYesterday") == DEFAULT_INFO_WEIGHT


def test_verified_stamp_is_parseable_and_not_in_the_future():
    assert table_age_days() >= 0


def test_in_memory_budget_admits_up_to_the_limit():
    budget = WeightBudget(limit=100, window_s=60)
    for _ in range(5):
        assert budget._try(20) == 0.0
    assert budget._try(20) > 0
    assert budget.headroom() == pytest.approx(0.0)


def test_shared_budget_is_visible_to_a_second_process(tmp_path):
    path = tmp_path / "rl.sqlite"
    first = SharedWeightBudget(path, limit=100, window_s=60)
    for _ in range(5):
        assert first._try(20) == 0.0

    # A brand new instance -- as a second CLI invocation would be -- must see
    # the spend, not start with a clean slate.
    second = SharedWeightBudget(path, limit=100, window_s=60)
    assert second._try(20) > 0


def test_shared_budget_forgets_spend_outside_the_window(tmp_path):
    path = tmp_path / "rl.sqlite"
    budget = SharedWeightBudget(path, limit=20, window_s=0.3)
    assert budget._try(20) == 0.0
    assert budget._try(20) > 0
    time.sleep(0.35)
    assert SharedWeightBudget(path, limit=20, window_s=0.3)._try(20) == 0.0


def test_shared_budget_counts_its_own_waits(tmp_path):
    budget = SharedWeightBudget(tmp_path / "rl.sqlite", limit=10, window_s=60)
    budget._try(10)
    budget._try(10)
    assert budget.waits == 1 and budget.wait_s > 0


def test_without_a_reservation_one_process_can_take_the_whole_account(tmp_path):
    # The starvation that failed 2,177 sweep requests three times running.
    poller = SharedWeightBudget(tmp_path / "rl.sqlite", limit=100, window_s=60, owner="capture")
    assert poller._try(100) == 0.0
    assert poller.reserved_by_others() == 0


def test_a_reservation_holds_budget_back_from_other_processes(tmp_path):
    path = tmp_path / "rl.sqlite"
    sweeper = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    poller = SharedWeightBudget(path, limit=100, window_s=60, owner="capture")

    with sweeper.reserve(60):
        assert poller.reserved_by_others() == 60
        # 60 reserved leaves 40: the poller is admitted under that and blocked
        # over it.
        assert poller._try(40) == 0.0
        assert poller._try(10) > 0


def test_the_holder_still_draws_on_the_full_limit(tmp_path):
    path = tmp_path / "rl.sqlite"
    sweeper = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    with sweeper.reserve(60):
        # Its own lease must not be subtracted from its own allowance, or the
        # reservation would throttle the very sweep it exists to protect.
        assert sweeper._try(90) == 0.0


def test_a_reservation_never_starves_the_other_process_completely(tmp_path):
    path = tmp_path / "rl.sqlite"
    greedy = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    poller = SharedWeightBudget(path, limit=100, window_s=60, owner="capture")
    with greedy.reserve(100_000):
        # Dropped tape cannot be recovered later, so the poller keeps a floor
        # no reservation can take.
        assert poller._try(int(100 * MIN_SHARE_FRACTION)) == 0.0


def test_a_reservation_is_released_even_when_the_sweep_raises(tmp_path):
    path = tmp_path / "rl.sqlite"
    sweeper = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    poller = SharedWeightBudget(path, limit=100, window_s=60, owner="capture")
    with pytest.raises(RuntimeError):
        with sweeper.reserve(60):
            raise RuntimeError("sweep died")
    assert poller.reserved_by_others() == 0


def test_a_stalled_reservation_expires_on_its_own(tmp_path):
    path = tmp_path / "rl.sqlite"
    sweeper = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    poller = SharedWeightBudget(path, limit=100, window_s=60, owner="capture")
    with sweeper.reserve(60, ttl_s=0.2):
        assert poller.reserved_by_others() == 60
        time.sleep(0.25)
        # A process killed mid-sweep leaves the row behind; it must not hold
        # the account hostage.
        assert poller.reserved_by_others() == 0


def test_progress_renews_the_lease_so_a_slow_sweep_keeps_its_share(tmp_path):
    path = tmp_path / "rl.sqlite"
    sweeper = SharedWeightBudget(path, limit=100, window_s=60, owner="sweep")
    poller = SharedWeightBudget(path, limit=100, window_s=60, owner="capture")
    with sweeper.reserve(60, ttl_s=0.3):
        time.sleep(0.2)
        assert sweeper._try(1) == 0.0  # a request renews it
        time.sleep(0.2)
        assert poller.reserved_by_others() == 60


def test_in_memory_budget_still_supports_reserve(tmp_path):
    # Callers reserve unconditionally, so the no-op must exist on the base.
    budget = WeightBudget(limit=100)
    with budget.reserve(50):
        assert budget._try(100) == 0.0


def _client(handler, monkeypatch, budget) -> InfoClient:
    client = InfoClient(budget)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_info_client_retries_a_429_then_succeeds(tmp_path, monkeypatch):
    import asyncio

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={})
        return httpx.Response(200, json={"ok": True})

    budget = WeightBudget(limit=10_000)
    client = _client(handler, monkeypatch, budget)
    result = asyncio.run(client.post("meta"))
    assert result == {"ok": True}
    assert calls["n"] == 2
    # A 429 means our model of the limit was wrong; that has to be visible.
    assert client.throttled == 1


def test_info_client_honours_retry_after_over_its_own_backoff():
    assert THROTTLED_BACKOFF_S > 60, "a 429 must back off past the whole sliding window"


def test_info_client_gives_up_rather_than_looping_forever():
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    client = InfoClient(WeightBudget(limit=10_000))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="failed after"):
        asyncio.run(client.post("meta"))


def test_info_client_charges_weight_for_every_attempt():
    import asyncio

    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={})

    budget = WeightBudget(limit=10_000)
    client = InfoClient(budget)
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        asyncio.run(client.post("clearinghouseState", user="0x1"))
    # A retry is another request the server counts, so it must cost us too.
    spent = sum(w for _, w in budget._events)
    assert spent == attempts["n"] * weight_of("clearinghouseState")
