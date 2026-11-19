"""/desk/refresh never waits on the majors feed, and majors refreshes never stack up."""
from __future__ import annotations

import asyncio

import backend.orchestrator as orchmod
from backend.tests.test_desk_order_extract import _fixture_orch


async def test_desk_refresh_does_not_wait_for_majors_and_runs_one_majors_refresh(monkeypatch):
    calls = {"prices": 0}
    gate = asyncio.Event()

    async def slow_prices(mints):
        calls["prices"] += 1
        await gate.wait()          # a rate-limited price API
        return {}

    async def no_pool(mint):
        return None
    monkeypatch.setattr(orchmod, "live_prices", slow_prices)
    monkeypatch.setattr(orchmod, "find_pool", no_pool)
    orch, _ = _fixture_orch()
    orch.majors_enabled = True     # majors are frozen by default (meme-only co-pilot)

    async def market(pools):
        return {}
    orch._market = market
    for _ in range(5):
        r = await asyncio.wait_for(orch.refresh_market(), 1.0)
        assert r["ok"] is True
    await asyncio.sleep(0)
    assert calls["prices"] == 1
    gate.set()
    await asyncio.sleep(0.05)


async def test_jupiter_prices_one_call_for_many(monkeypatch):
    import httpx

    import backend.market.ohlcv as oh
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("ids"))
        return httpx.Response(200, json={"A1": {"usdPrice": 1.5}, "B2": {"usdPrice": 0}})

    async def nowait():
        return None
    monkeypatch.setattr(oh._jup_limiter, "wait", nowait)
    monkeypatch.setattr(oh, "_jup_backoff_until", 0.0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        out = await oh.jupiter_prices(["A1", "B2", "A1"], c)
    assert out == {"A1": 1.5} and seen == ["A1,B2"]
