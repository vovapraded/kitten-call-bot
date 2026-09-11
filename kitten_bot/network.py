"""Telegram transport diagnostics without tokens, request URLs or chat contents."""

import logging
import time

from telegram.error import BadRequest, Conflict, Forbidden, NetworkError
from telegram.ext import ExtBot
from telegram.request import HTTPXRequest

logger = logging.getLogger(__name__)
POLL_TIMEOUT = 30
API_METHODS = {
    "getMe",
    "deleteWebhook",
    "getUpdates",
    "setMyCommands",
    "getWebhookInfo",
    "sendPhoto",
    "sendMessage",
    "getChatMember",
}


class StartupRejected(RuntimeError):
    """A permanent bootstrap error that PTB must not retry indefinitely."""

    def __init__(self, method: str, kind: str):
        self.method = method
        self.kind = kind
        super().__init__(f"Telegram: {method}, {kind}")


def error_kind(exc: BaseException) -> str:
    """Report only the exception class, never its possibly credential-bearing text."""
    return type(exc.__cause__ if exc.__cause__ is not None else exc).__name__


class TelegramRequest(HTTPXRequest):
    def __init__(self, timeout: float = 30.0, *, polling: bool = False):
        super().__init__(
            connection_pool_size=1 if polling else 16,
            connect_timeout=timeout,
            read_timeout=timeout,
            write_timeout=timeout,
            pool_timeout=10,
            media_write_timeout=max(60, timeout),
        )
        self._last_warning: dict[str, float] = {}
        self._failed: set[str] = set()

    def _warn(self, api_method: str, kind: str) -> None:
        now = time.monotonic()
        self._failed.add(api_method)
        if api_method not in self._last_warning or now - self._last_warning[api_method] >= 60:
            logger.warning(
                "Telegram %s: %s. Проверьте доступ к api.telegram.org:443. "
                "Диагностика: python -m kitten_bot.doctor",
                api_method,
                kind,
            )
            self._last_warning[api_method] = now

    async def do_request(self, url: str, method: str, **kwargs) -> tuple[int, bytes]:
        name = url.rsplit("/", 1)[-1]
        api_method = name if name in API_METHODS else "Bot API"
        try:
            response = await super().do_request(url=url, method=method, **kwargs)
        except NetworkError as exc:
            self._warn(api_method, error_kind(exc))
            raise

        status, _ = response
        if status != 200:
            self._warn(api_method, f"HTTP {status}")
        else:
            if api_method in self._failed:
                logger.info("Соединение с Telegram восстановлено: %s", api_method)
                self._failed.discard(api_method)
                self._last_warning.pop(api_method, None)
        return response


class ReliableBot(ExtBot):
    """Keep PTB's retry/signal lifecycle, but fail fast on rejected bootstrap requests."""

    __slots__ = ("_polling_confirmed",)

    def __init__(self, token: str, timeout: float = 30.0):
        super().__init__(
            token=token,
            request=TelegramRequest(timeout),
            get_updates_request=TelegramRequest(timeout, polling=True),
        )
        self._polling_confirmed = False

    async def initialize(self) -> None:
        try:
            await super().initialize()
        except (BadRequest, Forbidden, Conflict) as exc:
            raise StartupRejected("getMe", type(exc).__name__) from None

    async def delete_webhook(self, *args, **kwargs) -> bool:
        try:
            return await super().delete_webhook(*args, **kwargs)
        except (BadRequest, Forbidden, Conflict) as exc:
            raise StartupRejected("deleteWebhook", type(exc).__name__) from None

    async def get_updates(self, *args, **kwargs):
        updates = await super().get_updates(*args, **kwargs)
        if not self._polling_confirmed:
            logger.info("Бот получает обновления из Telegram; подключение подтверждено")
            self._polling_confirmed = True
        return updates
