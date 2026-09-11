"""Verify actual HTTP/SOCKS routing, with TCP dial replaced before any network traffic."""

import asyncio
from unittest.mock import AsyncMock

import httpcore
import pytest
from telegram.error import NetworkError

from kitten_bot.app import build_application
from kitten_bot.config import Config
from kitten_bot.doctor import diagnose
from kitten_bot.photos import Photos
from kitten_bot.storage import Store

TOKEN = "123456:fake-proxy-test-token"


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
async def test_every_telegram_operation_uses_proxy_even_when_it_fails(
    monkeypatch,
    tmp_path,
    scheme,
    caplog,
):
    proxy = f"{scheme}://test-user:test-password@proxy.example:12345"
    # Explicit configuration must win over all inherited settings, including NO_PROXY.
    monkeypatch.setenv("HTTPS_PROXY", "http://wrong.example:9000")
    monkeypatch.setenv("NO_PROXY", "*")
    connect = AsyncMock(side_effect=httpcore.ConnectError("dial failed"))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    config = Config(token=TOKEN, telegram_proxy_url=proxy)
    store = Store(tmp_path / "db.sqlite3")
    app = build_application(config, store, Photos(config.kitten_dir))
    try:
        for operation in (
            app.bot.get_me(),
            app.bot.get_updates(timeout=0),
            app.bot.send_photo(chat_id=-1, photo=b"test photo", caption="test caption"),
        ):
            with pytest.raises(NetworkError):
                await asyncio.wait_for(operation, timeout=2)
        assert connect.await_count == 3
        for call in connect.await_args_list:
            assert call.args[:2] == ("proxy.example", 12345)
        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert TOKEN not in messages
        assert "test-password" not in messages
    finally:
        for request in app.bot._request:
            await request.shutdown()
        store.close()


@pytest.mark.parametrize("scheme", ["http", "socks5"])
async def test_doctor_uses_the_same_proxy_without_showing_address_or_credentials(
    monkeypatch,
    capsys,
    scheme,
):
    connect = AsyncMock(side_effect=httpcore.ConnectTimeout("unreachable proxy"))
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    proxy = f"{scheme}://private-user:private-password@proxy.example:12345"
    code = await diagnose(Config(token=TOKEN, telegram_proxy_url=proxy))
    assert code == 1
    connect.assert_awaited_once()
    assert connect.await_args.args[:2] == ("proxy.example", 12345)
    output = capsys.readouterr().out
    assert "настроенный прокси" in output
    for secret in ("private-user", "private-password", "proxy.example", TOKEN):
        assert secret not in output
