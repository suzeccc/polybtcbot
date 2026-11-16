"""Discover current and upcoming 5-minute BTC markets via Gamma."""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .config import CONFIG
from .models import Market

log = logging.getLogger(__name__)
_WINDOW_SEC = 300

_RETRY = dict(
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.5, max=4.0),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)


@dataclass
class DiscoveryResult:
    markets: list[Market]
    requested_slugs: set[str]
    failed_slugs: set[str]
    server_time: float | None


def _window_start_ts(now: float | None = None) -> int:
    t = int(time.time() if now is None else now)
    return t - (t % _WINDOW_SEC)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return value == 1


def _parse_array(value: object) -> list:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, list) else []


def _parse_end_ts(value: object, fallback: int) -> int:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except ValueError:
        return fallback


def _parse_market(event: dict, expected_slug: str) -> Market | None:
    slug = event.get("slug")
    try:
        start_ts = int(expected_slug.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return None
    if slug != expected_slug or not isinstance(event.get("markets"), list):
        return None

    for raw_market in event["markets"]:
        if not isinstance(raw_market, dict):
            continue
        market_slug = raw_market.get("slug") or slug
        if market_slug != expected_slug:
            continue
        try:
            outcomes = [str(item).strip().lower() for item in _parse_array(raw_market.get("outcomes"))]
            token_ids = [str(item).strip() if item is not None else "" for item in _parse_array(raw_market.get("clobTokenIds"))]
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if len(outcomes) != len(token_ids) or not token_ids or len(set(token_ids)) != len(token_ids):
            continue
        try:
            up_idx, down_idx = outcomes.index("up"), outcomes.index("down")
        except ValueError:
            continue

        condition_id = raw_market.get("conditionId") or raw_market.get("condition_id")
        if not raw_market.get("id") or not condition_id or not token_ids[up_idx] or not token_ids[down_idx]:
            continue
        end_ts = _parse_end_ts(raw_market.get("endDate"), start_ts + _WINDOW_SEC)
        if end_ts - start_ts != _WINDOW_SEC:
            log.warning("Unexpected BTC 5m market duration for %s: %s-%s", slug, start_ts, end_ts)
            return None

        return Market(
            slug=slug,
            condition_id=str(condition_id),
            up_token_id=token_ids[up_idx],
            down_token_id=token_ids[down_idx],
            start_ts=start_ts,
            end_ts=end_ts,
            active=_as_bool(event.get("active")) and _as_bool(raw_market.get("active")),
            closed=_as_bool(event.get("closed")) or _as_bool(raw_market.get("closed")),
            accepting_orders=_as_bool(raw_market.get("acceptingOrders")),
            enable_order_book=_as_bool(raw_market.get("enableOrderBook")),
        )
    return None


def _server_time(resp: httpx.Response) -> float | None:
    value = resp.headers.get("date")
    if not value:
        return None
    try:
        server_time = parsedate_to_datetime(value).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        cache_age = max(0, int(resp.headers.get("age", "0")))
    except (TypeError, ValueError):
        cache_age = 0
    return server_time + cache_age


@retry(**_RETRY)
async def _fetch_event(client: httpx.AsyncClient, slug: str) -> tuple[dict | None, float | None]:
    resp = await client.get(
        f"{CONFIG.gamma_api_base}/events",
        params={"slug": slug},
        timeout=5.0,
    )
    resp.raise_for_status()
    data = resp.json()
    event = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
    return event, _server_time(resp)


async def discover_markets(
    client: httpx.AsyncClient | None = None,
    *,
    now: float | None = None,
) -> DiscoveryResult:
    """Fetch the current market and all 5-minute slugs within the lookahead."""
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(
            headers={"User-Agent": "btcbot/0.1"},
            timeout=httpx.Timeout(5.0, connect=3.0),
        )
    assert client is not None
    current_start = _window_start_ts(now)
    last_start = current_start + int(math.ceil(CONFIG.discovery_lookahead_sec / _WINDOW_SEC)) * _WINDOW_SEC
    slugs = [f"btc-updown-5m-{start}" for start in range(current_start, last_start + 1, _WINDOW_SEC)]
    markets: list[Market] = []
    failed_slugs: set[str] = set()
    server_times: list[float] = []
    try:
        for slug in slugs:
            try:
                event, server_ts = await _fetch_event(client, slug)
                if server_ts is not None:
                    server_times.append(server_ts)
                if event:
                    market = _parse_market(event, slug)
                    if market:
                        markets.append(market)
            except Exception:
                failed_slugs.add(slug)
                log.warning("Gamma discovery request failed for %s", slug, exc_info=True)
    finally:
        if owns_client:
            await client.aclose()

    return DiscoveryResult(
        markets=markets,
        requested_slugs=set(slugs),
        failed_slugs=failed_slugs,
        server_time=server_times[-1] if server_times else None,
    )
