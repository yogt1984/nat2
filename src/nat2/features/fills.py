"""Position deltas from the public trade tape.

HL's public `trades` channel carries **both counterparty addresses** on every
print. That is the whole ballgame for this system: the venue's entire fill
flow is observable from one non-user subscription, so the registry needs no
per-address subscriptions at all.

This matters because HL caps user tracking hard -- measured 2026-08-07, a
single websocket accepts **15 tracked users** ("Cannot track more than 15
total users"), so a 2,000-wallet registry would have needed ~146 connections
against a per-IP connection limit. The trade tape sidesteps the cap
completely and covers every wallet, not just the registry.

Convention, verified against `userFills` by matching `tid` (8/8 agreement):

    users[0] is the BUYER, users[1] is the SELLER

`trade.side` is the aggressor's side and is *not* the direction of either
counterparty -- signing a position from it inverts the map for every passive
fill. Only the `users` ordering carries direction.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass(frozen=True)
class Delta:
    address: str
    coin: str
    dsz: float          # signed size change: positive is bought
    notional: float
    last_px: float
    fills: int


def signed_parties(trade: dict) -> list[tuple[str, float]]:
    """(address, signed size) for both sides of one print."""
    users = trade.get("users") or []
    if len(users) != 2:
        return []
    size = abs(float(trade.get("sz") or 0))
    if size == 0:
        return []
    return [(users[0], +size), (users[1], -size)]


def deltas(trades, addresses: set[str] | None = None) -> list[Delta]:
    """Aggregate signed size changes per (address, coin) over a trade stream.

    With `addresses` given, restricts to the registry; without it, every
    address on the tape -- which is what makes whole-venue reconstruction
    possible rather than registry-limited.
    """
    acc: dict[tuple[str, str], list] = defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    for trade in trades:
        coin = trade.get("coin")
        price = float(trade.get("px") or 0)
        if not coin or price <= 0:
            continue
        for address, signed in signed_parties(trade):
            if addresses is not None and address not in addresses:
                continue
            entry = acc[(address, coin)]
            entry[0] += signed
            entry[1] += abs(signed) * price
            entry[2] = price
            entry[3] += 1
    return [
        Delta(address, coin, dsz, notional, last_px, fills)
        for (address, coin), (dsz, notional, last_px, fills) in acc.items()
    ]


def flatten(records) -> list[dict]:
    """WORM records for `hl.trades` hold a list of prints per message."""
    out = []
    for record in records:
        payload = record.get("payload")
        if isinstance(payload, list):
            out.extend(p for p in payload if isinstance(p, dict))
    return out


def participation(trades, addresses: set[str]) -> dict:
    """How much of the tape our registry is actually on."""
    total = seen = 0
    notional_total = notional_seen = 0.0
    for trade in trades:
        price = float(trade.get("px") or 0)
        size = abs(float(trade.get("sz") or 0))
        users = set(trade.get("users") or [])
        total += 1
        notional_total += price * size
        if users & addresses:
            seen += 1
            notional_seen += price * size
    return {
        "trades": total,
        "registry_trades": seen,
        "trade_frac": seen / total if total else 0.0,
        "notional": notional_total,
        "registry_notional": notional_seen,
        "notional_frac": notional_seen / notional_total if notional_total else 0.0,
    }
