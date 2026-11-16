"""Order-book readiness depends on complete snapshots and fresh updates."""

import json
import time

import pytest

from btcbot.feeds.polymarket_ws import PolymarketFeed


@pytest.mark.asyncio
async def test_book_snapshot_sets_midpoint_and_stale_or_disconnect_invalidates_it():
    feed = PolymarketFeed()
    await feed.set_token_ids({"up"})
    await feed._handle_message(json.dumps({
        "event_type": "book",
        "asset_id": "up",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.50", "size": "8"}],
    }))

    assert feed.get_price("up") == pytest.approx(0.45)
    assert feed.has_fresh_book("up", 5)
    assert not feed.has_fresh_book("up", 0, now=feed._books["up"].received_at + 1)

    feed._clear_books()
    assert feed.get_price("up") is None
    assert not feed.has_fresh_book("up", 5)


@pytest.mark.asyncio
async def test_price_change_without_initial_snapshot_cannot_make_book_ready():
    feed = PolymarketFeed()
    await feed.set_token_ids({"up"})
    await feed._handle_message(json.dumps({
        "event_type": "price_change",
        "price_changes": [{
            "asset_id": "up", "price": "0.40", "size": "10", "side": "BUY",
            "best_bid": "0.40", "best_ask": "0.50",
        }],
    }))

    assert not feed.has_fresh_book("up", 5)
    assert feed.get_price("up") is None


@pytest.mark.asyncio
async def test_new_payload_shape_and_incremental_updates_use_snapshot_state():
    feed = PolymarketFeed()
    await feed._handle_message(json.dumps({
        "type": "book",
        "payload": {
            "assetId": "up",
            "bids": [{"price": "0.40", "size": "10"}],
            "asks": [{"price": "0.50", "size": "8"}],
        },
    }))
    await feed._handle_message(json.dumps({
        "type": "price_change",
        "payload": {"priceChanges": [{
            "assetId": "up", "price": "0.45", "size": "2", "side": "BUY",
            "bestBid": "0.45", "bestAsk": "0.50",
        }]},
    }))

    assert feed.has_fresh_book("up", 5)
    assert feed.get_price("up") == pytest.approx(0.475)


@pytest.mark.asyncio
async def test_token_changes_use_dynamic_subscribe_and_unsubscribe():
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.closed = False

        async def send(self, message):
            self.sent.append(json.loads(message))

        async def close(self):
            self.closed = True

    feed = PolymarketFeed()
    socket = FakeSocket()
    feed._ws = socket
    feed._desired_tokens = {"old", "keep"}
    feed._current_tokens = {"old", "keep"}
    await feed.set_token_ids({"new", "keep"})

    assert socket.sent[0]["operation"] == "subscribe"
    assert socket.sent[0]["assets_ids"] == ["new"]
    assert socket.sent[1]["operation"] == "unsubscribe"
    assert socket.sent[1]["assets_ids"] == ["old"]
    assert not socket.closed


@pytest.mark.asyncio
async def test_stale_active_book_reconnects_and_is_throttled():
    class FakeSocket:
        def __init__(self):
            self.close_count = 0

        async def close(self):
            self.close_count += 1

    feed = PolymarketFeed()
    socket = FakeSocket()
    feed._ws = socket
    await feed._handle_message(json.dumps({
        "event_type": "book",
        "asset_id": "up",
        "bids": [{"price": "0.40", "size": "10"}],
        "asks": [{"price": "0.50", "size": "8"}],
    }))
    feed._books["up"].received_at -= 6

    await feed.reconnect_if_stale({"up"}, 5)
    await feed.reconnect_if_stale({"up"}, 5)

    assert socket.close_count == 1


@pytest.mark.asyncio
async def test_failed_dynamic_subscribe_keeps_socket_and_can_retry(monkeypatch):
    class FakeSocket:
        def __init__(self):
            self.sent = []
            self.fail = True
            self.closed = False

        async def send(self, message):
            if self.fail:
                self.fail = False
                raise OSError("temporary send failure")
            self.sent.append(json.loads(message))

    feed = PolymarketFeed()
    socket = FakeSocket()
    feed._ws = socket
    feed._desired_tokens = {"keep"}
    feed._current_tokens = {"keep"}
    await feed.set_token_ids({"new", "keep"})
    assert not socket.closed
    assert "new" not in feed._current_tokens

    feed._subscription_sent_at["new"] = time.monotonic() - 100
    await feed.set_token_ids({"new", "keep"})

    assert socket.sent[0]["operation"] == "subscribe"
    assert socket.sent[0]["assets_ids"] == ["new"]
    assert "new" in feed._current_tokens
    assert not socket.closed


@pytest.mark.asyncio
async def test_pending_subscription_retries_without_a_fresh_book():
    class FakeSocket:
        def __init__(self):
            self.sent = []

        async def send(self, message):
            self.sent.append(json.loads(message))

    feed = PolymarketFeed()
    socket = FakeSocket()
    feed._ws = socket
    feed._desired_tokens = {"up"}
    feed._current_tokens = {"up"}
    feed._subscription_sent_at["up"] = time.monotonic() - 10

    await feed.retry_stale_subscriptions({"up"}, 5)

    assert socket.sent == [{
        "assets_ids": ["up"],
        "operation": "subscribe",
        "custom_feature_enabled": True,
    }]
