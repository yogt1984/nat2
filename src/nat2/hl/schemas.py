"""The only place that knows Hyperliquid's payload shapes.

Capture stores payloads **raw and unvalidated**.  That is deliberate: a schema
change at the venue must never cause the daemon to drop records it could have
kept.  Validation belongs on the read side, where a rejected record can be
inspected instead of lost.  What lives here is the minimum needed to capture
correctly -- which stream a message belongs to, and where its exchange clock
is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from nat2.core.clock import ms_to_ns

WS_URL = "wss://api.hyperliquid.xyz/ws"
INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL_TESTNET = "wss://api.hyperliquid-testnet.xyz/ws"
INFO_URL_TESTNET = "https://api.hyperliquid-testnet.xyz/info"


def _max_trade_time(data) -> int | None:
    times = [t.get("time") for t in data or [] if isinstance(t, dict)]
    times = [t for t in times if t is not None]
    return ms_to_ns(max(times)) if times else None


def _field_time(data) -> int | None:
    return ms_to_ns(data.get("time")) if isinstance(data, dict) else None


def _no_clock(_data) -> None:
    """Streams HL publishes without an exchange timestamp.

    ``t_event`` is genuinely unknown for these, and the audit must not treat
    that as a fault.  It does mean ``t_ingest`` is the only clock they have,
    which is exactly why the poller cadence is recorded alongside them.
    """
    return None


@dataclass(frozen=True)
class StreamSpec:
    name: str            # our stream name, and its directory in the store
    channel: str         # HL websocket channel, or "" for polled streams
    sub_type: str        # HL subscription type
    per_coin: bool
    event_time: Callable[[object], int | None]
    has_event_clock: bool
    cadence_hint_s: float  # typical seconds between records; staleness baseline


STREAMS: dict[str, StreamSpec] = {
    "hl.trades": StreamSpec(
        "hl.trades", "trades", "trades", True, _max_trade_time, True, 5.0
    ),
    "hl.l2book": StreamSpec(
        "hl.l2book", "l2Book", "l2Book", True, _field_time, True, 1.0
    ),
    "hl.bbo": StreamSpec("hl.bbo", "bbo", "bbo", True, _field_time, True, 1.0),
    # Polled: one request returns mark, oracle, funding, OI and day volume for
    # the whole universe, which is cheaper on the rate-limit budget than a
    # per-coin activeAssetCtx subscription and gives a coherent cross-section.
    "hl.assetctxs": StreamSpec(
        "hl.assetctxs", "", "metaAndAssetCtxs", False, _no_clock, False, 10.0
    ),
}

CHANNEL_TO_STREAM = {s.channel: name for name, s in STREAMS.items() if s.channel}


def asset_contexts(payload) -> list[dict]:
    """Flatten a ``metaAndAssetCtxs`` response into per-coin context dicts.

    HL returns ``[meta, ctxs]`` as two parallel arrays; pairing them here is
    the one place that ordering assumption is allowed to live.
    """
    if not (isinstance(payload, list) and len(payload) == 2):
        return []
    meta, ctxs = payload
    universe = meta.get("universe", []) if isinstance(meta, dict) else []
    out = []
    for asset, ctx in zip(universe, ctxs):
        if not isinstance(ctx, dict):
            continue
        out.append({"coin": asset.get("name"), **ctx})
    return out
