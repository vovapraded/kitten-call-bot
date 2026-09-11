import httpx
import pytest

from kitten_bot import doctor
from kitten_bot.config import Config

TOKEN = "123456:diagnostic-test-secret"


def mock_telegram(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        doctor.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.parametrize("webhook", ["", "https://example.test/private-webhook-secret"])
async def test_diagnostic_is_read_only_and_never_prints_credentials(monkeypatch, capsys, webhook):
    methods = []

    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        methods.append(method)
        result = {"id": 123, "username": "private_test_name", "is_bot": True}
        if method == "getWebhookInfo":
            result = {"url": webhook}
        return httpx.Response(200, json={"ok": True, "result": result})

    mock_telegram(monkeypatch, handler)
    assert await doctor.diagnose(Config(token=TOKEN)) == 0
    assert methods == ["getMe", "getWebhookInfo"]
    output = capsys.readouterr().out
    assert "getMe: OK" in output
    assert "getWebhookInfo: OK" in output
    for secret in (TOKEN, "private_test_name", "private-webhook-secret"):
        assert secret not in output


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError])
async def test_diagnostic_reports_network_failure_without_raw_exception(
    monkeypatch, capsys, exception
):
    def handler(request):
        raise exception(f"Failed: {request.url}")

    mock_telegram(monkeypatch, handler)
    assert await doctor.diagnose(Config(token=TOKEN)) == 1
    output = capsys.readouterr().out
    assert exception.__name__ in output
    assert TOKEN not in output


@pytest.mark.parametrize("status", [401, 404, 403, 429, 502, 302])
async def test_diagnostic_reports_rejected_response_without_body(monkeypatch, capsys, status):
    mock_telegram(
        monkeypatch, lambda request: httpx.Response(status, text=f"Secret server response: {TOKEN}")
    )
    assert await doctor.diagnose(Config(token=TOKEN)) == 1
    output = capsys.readouterr().out
    assert TOKEN not in output
    assert "отклонил BOT_TOKEN" in output if status in {401, 404} else f"HTTP {status}" in output


@pytest.mark.parametrize("body", [b"html", b"[]", b'{"ok":false}', b'{"ok":true,"result":false}'])
async def test_diagnostic_rejects_invalid_success_response(monkeypatch, capsys, body):
    mock_telegram(monkeypatch, lambda request: httpx.Response(200, content=body))
    assert await doctor.diagnose(Config(token=TOKEN)) == 1
    assert "неожиданный ответ" in capsys.readouterr().out
