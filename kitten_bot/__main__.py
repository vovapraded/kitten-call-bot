"""Run a single long-polling worker with graceful shutdown."""

import logging
import sys

from telegram import Update
from telegram.error import InvalidToken

from .app import build_application
from .config import Config
from .network import POLL_TIMEOUT, StartupRejected
from .photos import Photos
from .storage import Store


def main() -> int:
    store = None
    try:
        config = Config.from_env()
        logging.basicConfig(
            level=config.log_level,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        # Third-party log records may contain Bot API URLs or raw updates. Our
        # handlers report exception types without exposing tokens/chat contents.
        for name in ("httpx", "httpcore", "telegram"):
            logging.getLogger(name).disabled = True

        class NoLibraryLogs(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return not record.name.startswith(("httpx", "httpcore", "telegram"))

        for handler in logging.getLogger().handlers:
            handler.addFilter(NoLibraryLogs())
        photos = Photos(config.kitten_dir, cache_dir=config.db_path.parent / "cats")
        store = Store(config.db_path)
        app = build_application(config, store, photos)
        logging.getLogger(__name__).info(
            "Подключаюсь к Telegram (сетевой таймаут: %s с). "
            "При временной недоступности соединение будет восстановлено автоматически.",
            config.telegram_timeout,
        )
        if config.telegram_proxy_url:
            logging.getLogger(__name__).info("Для Telegram настроен прокси")
        app.run_polling(
            allowed_updates=[Update.MESSAGE, Update.MY_CHAT_MEMBER],
            drop_pending_updates=False,
            timeout=POLL_TIMEOUT,
            bootstrap_retries=-1,
        )
    except InvalidToken:
        print("Telegram отклонил BOT_TOKEN. Проверьте токен из @BotFather в .env.", file=sys.stderr)
        return 1
    except StartupRejected as exc:
        print(
            f"Telegram отклонил запуск: {exc.method}, {exc.kind}. "
            "Запустите python -m kitten_bot.doctor для проверки сети и настроек.",
            file=sys.stderr,
        )
        return 1
    except ValueError as exc:
        # Only our validated messages are printed; dependency errors may include secrets.
        if str(exc).startswith(
            (
                "Задайте BOT_TOKEN",
                "LOG_LEVEL:",
                "В KITTEN_DIR",
                "TELEGRAM_TIMEOUT:",
                "TELEGRAM_PROXY_URL",
            )
        ):
            print(str(exc), file=sys.stderr)
        else:
            print("Ошибка конфигурации. Проверьте BOT_TOKEN и пути к данным/фото.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"Бот остановлен ({type(exc).__name__}). Проверьте настройки и сеть.", file=sys.stderr
        )
        return 1
    finally:
        if store is not None:
            store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
