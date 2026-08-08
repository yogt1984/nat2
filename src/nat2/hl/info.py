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


MAX_ATTEMPTS = 4
# A 429 means our model of the limit is wrong, so back off past the whole
# sliding window rather than retrying inside it.
THROTTLED_BACKOFF_S = 61.0


class InfoClient:
    def __init__(self, budget: WeightBudget, testnet: bool = False, timeout: float = 30.0):
        self.url = INFO_URL_TESTNET if testnet else INFO_URL
        self.budget = budget
        self.throttled = 0
        self._client = httpx.AsyncClient(timeout=timeout)

    async def post(self, info_type: str, **body) -> object:
        payload = {"type": info_type, **body}
        last: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            await self.budget.acquire_async(weight_of(info_type))
            try:
                resp = await self._client.post(self.url, json=payload)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPStatusError as exc:
                last = exc
                if exc.response.status_code != 429:
                    await asyncio.sleep(0.5 * 2**attempt)
                    continue
                # Our accounting says there was room, so the server disagrees
                # with our model of the limit -- wait out the whole window
                # rather than hammering it with a short backoff.
                retry_after = exc.response.headers.get("retry-after")
                delay = float(retry_after) if retry_after else THROTTLED_BACKOFF_S
                self.throttled += 1
                await asyncio.sleep(delay)
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                await asyncio.sleep(0.5 * 2**attempt)
        raise RuntimeError(f"info {info_type} failed after {MAX_ATTEMPTS} attempts: {last}")

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
