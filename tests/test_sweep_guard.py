"""A failed sweep must never replace a good map.

On 2026-08-08, snapshots 4, 5 and 6 each had all 2,177 requests fail, and each
then called `replace_positions([])` — deleting the table and inserting nothing.
Three transient outages silently destroyed the map, and every downstream
measurement went on reading an empty table while reporting success.

The rule these tests defend: a sweep that learned nothing is not evidence that
there is nothing.
"""

from __future__ import annotations

import asyncio
from collections import Counter

import httpx
import pytest

from nat2.core.errors import reason, top_reasons
from nat2.core.registry import Registry
from nat2.features.liqmath import Position
from nat2.hl.info import InfoClient
from nat2.hl.ratelimit import WeightBudget
from nat2.io.snapshot import MAX_ERROR_FRACTION, refusal, sweep

STATE = {
    "crossMarginSummary": {"accountValue": "1000"},
    "crossMaintenanceMarginUsed": "10",
    "assetPositions": [{
        "position": {"coin": "BTC", "szi": "-2", "positionValue": "200",
                     "maxLeverage": 40, "liquidationPx": "150",
                     "leverage": {"type": "cross", "value": 3}},
    }],
}


def _position(address="0xold") -> Position:
    return Position(address=address, coin="BTC", szi=1.0, mark=100.0, max_leverage=40,
                    margin_type="cross", account_value=50.0, maint_margin=1.0,
                    liquidation_px=95.0)


def _client(handler) -> InfoClient:
    client = InfoClient(WeightBudget(limit=10_000))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


# --- the refusal rule itself ----------------------------------------------

def test_total_failure_is_refused():
    assert "refusing to erase the map" in refusal(wallets=100, holders=0, errors=100)


def test_a_clean_sweep_that_found_nobody_is_allowed():
    # Zero holders with zero errors is a real answer: nobody held anything.
    assert refusal(wallets=100, holders=0, errors=0) is None


def test_a_clean_sweep_is_allowed():
    assert refusal(wallets=100, holders=40, errors=0) is None


def test_a_few_failures_are_tolerated():
    assert refusal(wallets=100, holders=40, errors=5) is None


def test_majority_failure_is_refused_even_with_some_holders():
    # Publishing this would drop every wallet we merely could not reach.
    assert refusal(wallets=100, holders=10, errors=60) is not None


def test_the_boundary_is_not_refused():
    at_limit = int(100 * MAX_ERROR_FRACTION)
    assert refusal(wallets=100, holders=10, errors=at_limit) is None
    assert refusal(wallets=100, holders=10, errors=at_limit + 1) is not None


def test_an_empty_address_list_is_refused():
    assert refusal(wallets=0, holders=0, errors=0) is not None


# --- the guard in the real sweep ------------------------------------------

def test_a_totally_failed_sweep_leaves_the_previous_map_intact(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(), "published")])

    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    result = asyncio.run(sweep(registry, _client(always_500), ["0xa", "0xb", "0xc"]))

    assert "refused" in result
    assert result["positions"] == 0
    # The map that existed before is still there. This is the whole point.
    assert [p.address for p in registry.positions()] == ["0xold"]


def test_a_refused_sweep_is_recorded_as_refused(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(), "published")])

    def always_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={})

    asyncio.run(sweep(registry, _client(always_500), ["0xa"]))
    snapshot = registry.last_snapshot()
    assert snapshot["positions"] == 0
    assert snapshot["refused"]


def test_a_successful_sweep_still_replaces(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(address="0xstale"), "published")])

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STATE)

    result = asyncio.run(sweep(registry, _client(ok), ["0xa", "0xb"]))

    assert "refused" not in result
    assert result["holders"] == 2 and result["positions"] == 2
    assert {p.address for p in registry.positions()} == {"0xa", "0xb"}
    assert "0xstale" not in {p.address for p in registry.positions()}


def test_a_partly_failed_sweep_below_the_limit_still_publishes(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500 if calls["n"] % 4 == 0 else 200, json=STATE)

    result = asyncio.run(sweep(registry, _client(flaky), [f"0x{i}" for i in range(20)]))
    assert "refused" not in result
    assert registry.positions()


def test_a_sweep_of_no_addresses_does_not_wipe_the_map(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    registry.replace_positions([(_position(), "published")])

    def ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STATE)

    result = asyncio.run(sweep(registry, _client(ok), []))
    assert "refused" in result
    assert registry.positions()


# --- attribution -----------------------------------------------------------

def test_failures_are_reported_with_a_reason(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"retry-after": "0"}, json={})

    result = asyncio.run(sweep(registry, _client(rate_limited), ["0xa"]))
    assert result["errors"] == 1
    assert "429" in result["why"], "a failure count without a reason is not a diagnosis"


def test_reason_extracts_the_status_from_an_http_error():
    response = httpx.Response(429, request=httpx.Request("POST", "http://x"))
    exc = httpx.HTTPStatusError("rate limited", request=response.request, response=response)
    assert reason(exc) == "HTTPStatusError 429"


def test_reason_finds_a_status_buried_in_a_runtime_error():
    exc = RuntimeError("info userFills failed after 4 attempts: Client error '429 Too Many'")
    assert reason(exc) == "RuntimeError 429"


def test_reason_falls_back_to_the_exception_type():
    assert reason(ValueError("no status here")) == "ValueError"
    assert reason(TimeoutError()) == "TimeoutError"


def test_reason_does_not_invent_a_status_from_arbitrary_digits():
    # "2177 wallets" must not be read as an HTTP status.
    assert reason(ValueError("failed for 2177 wallets")) == "ValueError"


def test_top_reasons_summarises_the_dominant_failures():
    counter = Counter({"RuntimeError 429": 2177, "TimeoutError": 3})
    summary = top_reasons(counter)
    assert summary.startswith("RuntimeError 429 x2177")
    assert top_reasons(Counter()) == ""


@pytest.mark.parametrize("errors,wallets,expected", [(0, 0, True), (1, 1, True), (0, 1, False)])
def test_refusal_is_decided_only_by_counts(errors, wallets, expected):
    assert (refusal(wallets, 0, errors) is not None) is expected
