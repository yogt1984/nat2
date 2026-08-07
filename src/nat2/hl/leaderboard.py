"""The wallet universe.

HL publishes a leaderboard of ~41k addresses with equity and windowed volume.
It is not part of the info API and costs no request weight, so it is the cheap
way to seed a registry -- but it seeds two different registries for two
different jobs:

  by equity   who holds size            -> the liquidation map
  by volume   who actually trades       -> the skill cohort

Measured 2026-08-07: top 2,000 by equity hold positions in only 28% of cases
but cover far more notional; top 2,000 by weekly volume are active in 46% of
cases and cover much less. Using one seed for both jobs gets one of them wrong.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


@dataclass(frozen=True)
class LeaderboardRow:
    address: str
    account_value: float
    vlm_day: float
    vlm_week: float
    pnl_month: float
    pnl_all: float


def _window(row: dict, name: str) -> dict:
    for label, stats in row.get("windowPerformances", []):
        if label == name:
            return stats
    return {}


def _float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


async def fetch(timeout: float = 120.0) -> list[LeaderboardRow]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.get(LEADERBOARD_URL)
        resp.raise_for_status()
        payload = resp.json()
    return parse(payload)


def parse(payload: dict) -> list[LeaderboardRow]:
    rows = []
    for row in payload.get("leaderboardRows", []):
        rows.append(
            LeaderboardRow(
                address=row["ethAddress"],
                account_value=_float(row.get("accountValue")),
                vlm_day=_float(_window(row, "day").get("vlm")),
                vlm_week=_float(_window(row, "week").get("vlm")),
                pnl_month=_float(_window(row, "month").get("pnl")),
                pnl_all=_float(_window(row, "allTime").get("pnl")),
            )
        )
    return rows


def seed(rows: list[LeaderboardRow], top_equity: int, top_volume: int) -> dict[str, str]:
    """Union of both seeds, tagged with which one(s) selected each address."""
    by_equity = sorted(rows, key=lambda r: -r.account_value)[:top_equity]
    by_volume = sorted(rows, key=lambda r: -r.vlm_week)[:top_volume]
    tags: dict[str, str] = {}
    for row in by_equity:
        tags[row.address] = "equity"
    for row in by_volume:
        tags[row.address] = "both" if row.address in tags else "volume"
    return tags
