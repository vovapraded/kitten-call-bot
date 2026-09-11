"""Read-only checks against Telegram, using the same network route as the bot."""

import asyncio
import logging
import sys

import httpx

from .config import Config
from .network import error_kind


async def diagnose(config: Config) -> int:
    print(
        "Маршрут Telegram: настроенный прокси."
        if config.telegram_proxy_url
        else "Маршрут Telegram: по умолчанию."
    )
    async with httpx.AsyncClient(
        timeout=config.telegram_timeout,
        follow_redirects=False,
        proxy=config.telegram_proxy_url,
    ) as client:
        for method in ("getMe", "getWebhookInfo"):
            try:
                response = await client.post(f"https://api.telegram.org/bot{config.token}/{method}")
            except httpx.HTTPError as exc:
                print(
                    f"{method}: нет связи с Telegram ({error_kind(exc)}). "
                    "Проверьте исходящий HTTPS к api.telegram.org:443, DNS и настройки сети "
                    "виртуалки и прокси (TELEGRAM_PROXY_URL)."
                )
                return 1
            if response.status_code in {401, 404}:
                print(f"{method}: Telegram отклонил BOT_TOKEN. Проверьте токен из @BotFather.")
                return 1
            if response.status_code != 200:
                print(f"{method}: сервер ответил HTTP {response.status_code}.")
                return 1
            try:
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("ok") is not True:
                    raise ValueError
                result = payload["result"]
                if not isinstance(result, dict):
                    raise ValueError
            except (ValueError, KeyError):
                print(f"{method}: получен неожиданный ответ. Проверьте сеть и прокси.")
                return 1
            if method == "getMe":
                print("getMe: OK — сеть работает, Telegram принял токен.")
            elif result.get("url"):
                print("getWebhookInfo: OK — установлен webhook; бот отключит его при запуске.")
            else:
                print("getWebhookInfo: OK — webhook отсутствует.")
    print(
        "Разовые запросы прошли. Это не проверяет стабильность длительного соединения. "
        "После запуска дождитесь лога «Бот получает обновления из Telegram»."
    )
    return 0


def main() -> int:
    # This is a separate diagnostic process. Never emit HTTP URLs containing the token.
    logging.disable(logging.CRITICAL)
    try:
        config = Config.from_env()
        return asyncio.run(diagnose(config))
    except ValueError:
        print("Проверьте BOT_TOKEN, TELEGRAM_PROXY_URL, TELEGRAM_TIMEOUT и LOG_LEVEL в .env.")
        return 1
    except Exception as exc:
        print(f"Диагностика завершилась с ошибкой ({type(exc).__name__}).")
        return 1


if __name__ == "__main__":
    sys.exit(main())
