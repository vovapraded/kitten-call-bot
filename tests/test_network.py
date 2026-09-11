"""Exercise the actual PTB bootstrap and HTTPX stack without contacting Telegram."""

import asyncio
import logging
from collections import Counter
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from telegram.error import InvalidToken, TelegramError, TimedOut
from telegram.ext import Application
from telegram.request import HTTPXRequest

from kitten_bot import network
from kitten_bot.network import POLL_TIMEOUT, ReliableBot, StartupRejected

TOKEN = "123456789:FAKE_NETWORK_TEST_SECRET"
BOT_INFO = {"id": 123456789, "is_bot": True, "first_name": "Kitten", "username": "KittenBot"}
READY_TEXT = "подключение подтверждено"


def api_method(request):
    return request.url.path.rsplit("/", 1)[-1]


def success(method):
    result = BOT_INFO if method == "getMe" else [] if method == "getUpdates" else True
    return httpx.Response(200, json={"ok": True, "result": result})


def own_messages(caplog):
    return [record.getMessage() for record in caplog.records if record.name == network.__name__]


@pytest.fixture
async def bot_factory(monkeypatch):
    """Keep real request timeouts/serialization; replace only the network transport."""
    bots = []

    def make(handler, timeout=30):
        def build_client(request):
            return httpx.AsyncClient(
                **{
                    **request._client_kwargs,
                    "transport": httpx.MockTransport(handler),
                    "trust_env": False,
                }
            )

        monkeypatch.setattr(HTTPXRequest, "_build_client", build_client)
        bot = ReliableBot(TOKEN, timeout)
        bots.append(bot)
        return bot

    yield make
    for bot in bots:
        await bot.shutdown()
        # An initialization failure can leave HTTP clients open before PTB marks
        # the Application initialized. Close those clients in the fixture too.
        for request in bot._request:
            await request.shutdown()


@pytest.mark.parametrize("stage", ["getMe", "deleteWebhook"])
@pytest.mark.parametrize("timeout_error", [httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_bootstrap_recovers_after_more_than_three_timeouts(
    bot_factory, caplog, stage, timeout_error
):
    calls = Counter()

    def handler(request):
        method = api_method(request)
        calls[method] += 1
        if method == stage and calls[method] <= 5:
            raise timeout_error(f"failed {request.url}", request=request)
        return success(method)

    bot = bot_factory(handler)
    app = Application.builder().bot(bot).build()
    with caplog.at_level(logging.INFO, logger=network.__name__):
        try:
            await asyncio.wait_for(app._bootstrap_initialize(max_retries=-1), timeout=2)
            await asyncio.wait_for(
                app.updater._bootstrap(
                    max_retries=-1, webhook_url="", allowed_updates=None,
                    drop_pending_updates=False,
                ),
                timeout=2,
            )
        finally:
            await app.shutdown()

    assert calls[stage] == 6
    assert calls["deleteWebhook" if stage == "getMe" else "getMe"] == 1
    messages = own_messages(caplog)
    assert any(timeout_error.__name__ in message for message in messages)
    assert any("восстановлено" in message for message in messages)
    assert not any(READY_TEXT in message for message in messages)
    assert TOKEN not in "\n".join(messages)


@pytest.mark.parametrize("stage", ["getMe", "deleteWebhook"])
@pytest.mark.parametrize(
    ("status", "expected_error", "kind"),
    [
        (400, StartupRejected, "BadRequest"),
        (403, StartupRejected, "Forbidden"),
        (409, StartupRejected, "Conflict"),
        (401, InvalidToken, None),
        (404, InvalidToken, None),
    ],
)
async def test_permanent_bootstrap_error_does_not_retry_forever(
    bot_factory, caplog, stage, status, expected_error, kind
):
    calls = Counter()

    def handler(request):
        method = api_method(request)
        calls[method] += 1
        if method == stage:
            return httpx.Response(
                status,
                json={"ok": False, "description": f"rejected {TOKEN}", "error_code": status},
            )
        return success(method)

    bot = bot_factory(handler)
    app = Application.builder().bot(bot).build()
    try:
        with pytest.raises(expected_error) as caught:
            await asyncio.wait_for(app._bootstrap_initialize(max_retries=-1), timeout=2)
            await asyncio.wait_for(
                app.updater._bootstrap(max_retries=-1, webhook_url="", allowed_updates=None),
                timeout=2,
            )
    finally:
        await app.shutdown()

    assert calls[stage] == 1
    assert TOKEN not in "\n".join(own_messages(caplog))
    if expected_error is StartupRejected:
        assert caught.value.method == stage
        assert caught.value.kind == kind
        assert TOKEN not in str(caught.value)


async def test_polling_readiness_requires_successful_parsing(bot_factory, caplog):
    responses = iter(
        [
            httpx.Response(200, text="<html>unavailable</html>"),
            httpx.Response(200, json={"ok": True, "result": [{}]}),
            success("getUpdates"),
            success("getUpdates"),
        ]
    )

    def handler(request):
        method = api_method(request)
        return next(responses) if method == "getUpdates" else success(method)

    bot = bot_factory(handler)
    await bot.initialize()
    with caplog.at_level(logging.INFO, logger=network.__name__):
        with pytest.raises(TelegramError):
            await bot.get_updates()
        assert not any(READY_TEXT in message for message in own_messages(caplog))
        with pytest.raises(KeyError, match="update_id"):
            await bot.get_updates()
        assert not any(READY_TEXT in message for message in own_messages(caplog))
        assert await bot.get_updates() == ()
        assert await bot.get_updates() == ()
    assert sum(READY_TEXT in message for message in own_messages(caplog)) == 1


async def test_real_updater_continues_after_polling_timeout(bot_factory, caplog):
    calls = Counter()
    confirmed = asyncio.Event()
    blocked_request = asyncio.Event()

    async def handler(request):
        method = api_method(request)
        calls[method] += 1
        if method == "getUpdates":
            if parse_qs(request.content.decode()).get("timeout") == ["0"]:
                return success(method)  # PTB shutdown acknowledgement.
            if calls[method] == 1:
                raise httpx.ReadTimeout(f"failed {TOKEN}", request=request)
            if calls[method] == 2:
                asyncio.get_running_loop().call_soon(confirmed.set)
                return success(method)
            await blocked_request.wait()
        return success(method)

    bot = bot_factory(handler)
    app = Application.builder().bot(bot).build()
    with caplog.at_level(logging.INFO, logger=network.__name__):
        await app.initialize()
        try:
            await app.updater.start_polling(timeout=POLL_TIMEOUT, bootstrap_retries=-1)
            await asyncio.wait_for(confirmed.wait(), timeout=2)
            assert app.updater.running
            assert bot._polling_confirmed
        finally:
            if app.updater.running:
                await asyncio.wait_for(app.updater.stop(), timeout=2)
            await app.shutdown()
    messages = own_messages(caplog)
    assert sum(READY_TEXT in message for message in messages) == 1
    assert any("ReadTimeout" in message for message in messages)
    assert any("восстановлено: getUpdates" in message for message in messages)
    assert TOKEN not in "\n".join(messages)


@pytest.mark.parametrize("timeout", [30, 45])
async def test_timeouts_apply_to_both_http_clients_and_polling_overhead(bot_factory, timeout):
    observed = {}

    def handler(request):
        method = api_method(request)
        observed[method] = request.extensions["timeout"]
        return success(method)

    bot = bot_factory(handler, timeout)
    await bot.initialize()
    await bot.get_updates(timeout=POLL_TIMEOUT)
    assert observed["getMe"] == {
        "connect": timeout, "read": timeout, "write": timeout, "pool": 10,
    }
    assert observed["getUpdates"] == {
        "connect": timeout, "read": timeout + POLL_TIMEOUT, "write": timeout, "pool": 10,
    }
    polling, ordinary = bot._request
    assert polling is not ordinary
    assert polling._client_kwargs["limits"].max_connections == 1
    assert ordinary._client_kwargs["limits"].max_connections == 16
    assert ordinary._media_write_timeout == 60


async def test_warnings_are_safe_throttled_and_reset_after_recovery(
    bot_factory, monkeypatch, caplog
):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(network, "time", SimpleNamespace(monotonic=lambda: clock.now))
    failing = False

    def handler(request):
        if failing:
            raise httpx.ConnectTimeout(f"failed {request.url}", request=request)
        return success(api_method(request))

    bot = bot_factory(handler)
    await bot.initialize()
    with caplog.at_level(logging.INFO, logger=network.__name__):
        failing = True
        for clock.now in [0, 20, 61]:
            with pytest.raises(TimedOut):
                await bot.get_me()
        assert sum("ConnectTimeout" in message for message in own_messages(caplog)) == 2

        failing = False
        await bot.get_me()
        assert sum("восстановлено" in message for message in own_messages(caplog)) == 1

        failing = True
        with pytest.raises(TimedOut):
            await bot.get_me()
    messages = own_messages(caplog)
    assert sum("ConnectTimeout" in message for message in messages) == 3
    assert TOKEN not in "\n".join(messages)
    assert "https://" not in "\n".join(messages)


async def test_unknown_api_method_is_never_echoed_into_diagnostics(bot_factory, caplog):
    def handler(request):
        raise httpx.ConnectTimeout(f"failed {request.url}", request=request)

    bot = bot_factory(handler)
    with pytest.raises(TimedOut):
        await bot.request.do_request(url=f"https://api.telegram.org/{TOKEN}", method="POST")
    messages = own_messages(caplog)
    assert any("Bot API: ConnectTimeout" in message for message in messages)
    assert TOKEN not in "\n".join(messages)
