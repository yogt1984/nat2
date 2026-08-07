"""HL info endpoint -- the read side of the API.

Every call goes through the shared weight budget.  Nothing here retries
forever: a persistently failing info endpoint is a capture outage, and the
audit should see the hole rather than have it papered over.
"""

from __future__ import annotations

import asyncio

import httpx

from nat2.hl.ratelimit import WeightBudget, weight_of
from nat2.hl.schemas import INFO_URL, INFO_URL_TESTNET


class InfoClient:
    def __init__(self, budget: WeightBudget, testnet: bool = False, timeout: float = 10.0):
        self.url = INFO_URL_TESTNET if testnet else INFO_URL
        self.budget = budget
        self._client = httpx.AsyncClient(timeout=timeout)

    async def post(self, info_type: str, **body) -> object:
        await self.budget.acquire_async(weight_of(info_type))
        payload = {"type": info_type, **body}
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = await self._client.post(self.url, json=payload)
                resp.raise_for_status()
                return resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError(f"info {info_type} failed after 3 attempts: {last}")

    async def meta(self) -> dict:
        return await self.post("meta")

    async def meta_and_asset_ctxs(self) -> list:
        return await self.post("metaAndAssetCtxs")

    async def clearinghouse_state(self, address: str) -> dict:
        return await self.post("clearinghouseState", user=address)

    async def universe(self, min_day_volume: float = 0.0) -> list[str]:
        """Tradable perp names, newest universe, delisted assets dropped.

        Universe is rebuilt from `meta` on every run rather than configured,
        because HL lists and delists; a hardcoded coin list silently captures
        a stale world.
        """
        meta, ctxs = await self.meta_and_asset_ctxs()
        out = []
        for asset, ctx in zip(meta.get("universe", []), ctxs):
            if asset.get("isDelisted"):
                continue
            if float(ctx.get("dayNtlVlm", 0) or 0) < min_day_volume:
                continue
            out.append(asset["name"])
        return out

    async def aclose(self) -> None:
        await self._client.aclose()
