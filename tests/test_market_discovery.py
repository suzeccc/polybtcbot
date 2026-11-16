"""Gamma discovery must find and validate upcoming market windows."""

from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

import btcbot.market_discovery as discovery


def _event(slug: str, *, closed=False, accepting=True, token_ids=None):
    start = int(slug.rsplit("-", 1)[1])
    end = datetime.fromtimestamp(start + 300, timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "id": "event-" + slug,
        "slug": slug,
        "active": True,
        "closed": closed,
        "markets": [{
            "id": "market-" + slug,
            "slug": slug,
            "conditionId": "condition-" + slug,
            "outcomes": '["Down", "Up"]',
            "clobTokenIds": token_ids or '["down-token", "up-token"]',
            "startDate": "not-the-window-start",
            "endDate": end,
            "active": True,
            "closed": closed,
            "acceptingOrders": accepting,
            "enableOrderBook": True,
        }],
    }


@pytest.mark.asyncio
async def test_scans_current_and_six_future_windows_with_named_tokens(monkeypatch):
    monkeypatch.setattr(
        discovery, "CONFIG", replace(discovery.CONFIG, discovery_lookahead_sec=1800)
    )
    requested = []

    async def handler(request):
        slug = request.url.params["slug"]
        requested.append(slug)
        return httpx.Response(200, json=[_event(slug)], headers={"date": "Mon, 16 Nov 2026 07:45:00 GMT"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await discovery.discover_markets(client, now=1800000123)
    await client.aclose()

    assert len(requested) == 7
    assert len({market.slug for market in result.markets}) == 7
    assert result.markets[0].up_token_id == "up-token"
    assert result.markets[0].down_token_id == "down-token"
    assert result.markets[0].end_ts - result.markets[0].start_ts == 300
    assert result.markets[0].is_tradable
    assert result.server_time is not None


@pytest.mark.asyncio
async def test_closed_or_non_accepting_event_is_never_tradable(monkeypatch):
    monkeypatch.setattr(
        discovery, "CONFIG", replace(discovery.CONFIG, discovery_lookahead_sec=0)
    )
    start = discovery._window_start_ts(1800000123)

    async def handler(request):
        return httpx.Response(200, json=[_event(f"btc-updown-5m-{start}", accepting=False)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await discovery.discover_markets(client, now=1800000123)
    await client.aclose()

    assert len(result.markets) == 1
    assert not result.markets[0].is_tradable


@pytest.mark.asyncio
@pytest.mark.parametrize("token_ids", ['["", "up"]', '[null, "up"]'])
async def test_invalid_or_missing_token_ids_are_rejected(monkeypatch, token_ids):
    monkeypatch.setattr(
        discovery, "CONFIG", replace(discovery.CONFIG, discovery_lookahead_sec=0)
    )
    start = discovery._window_start_ts(1800000123)

    async def handler(request):
        return httpx.Response(200, json=[_event(f"btc-updown-5m-{start}", token_ids=token_ids)])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await discovery.discover_markets(client, now=1800000123)
    await client.aclose()

    assert result.markets == []


@pytest.mark.asyncio
async def test_failed_gamma_request_is_reported_without_stopping_scan(monkeypatch):
    monkeypatch.setattr(
        discovery, "CONFIG", replace(discovery.CONFIG, discovery_lookahead_sec=0)
    )

    async def fail(_client, slug):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr(discovery, "_fetch_event", fail)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    result = await discovery.discover_markets(client, now=1800000123)
    await client.aclose()

    assert len(result.failed_slugs) == 1
    assert result.markets == []
