"""Startup configuration and per-chat defaults."""

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_COUNT = 20
DEFAULT_WINDOW = 40
DEFAULT_MESSAGE = "Котеночки, вы уверены что хотите переписываться, а не созвониться?)"
MAX_COUNT = 10_000
MAX_WINDOW = 1440
MAX_CAPTION = 1024


def validate_message(message: str) -> str:
    message = message.strip()
    if not message or len(message.encode("utf-16-le")) // 2 > MAX_CAPTION:
        raise ValueError(
            "Текст должен содержать от 1 до 1024 символов (эмодзи могут занимать два)."
        )
    return message


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    db_path: Path = Path("data/bot.sqlite3")
    kitten_dir: Path = Path(__file__).resolve().parent.parent / "assets" / "kittens"
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("BOT_TOKEN", "").strip()
        if not token or token == "paste_your_bot_token_here" or ":" not in token:
            raise ValueError("Задайте BOT_TOKEN: получите токен у @BotFather и сохраните в .env.")
        level = os.environ.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL: DEBUG, INFO, WARNING, ERROR или CRITICAL.")
        return cls(
            token=token,
            db_path=Path(os.environ.get("DB_PATH", "data/bot.sqlite3")),
            kitten_dir=Path(os.environ.get("KITTEN_DIR", str(cls.kitten_dir))),
            log_level=level,
        )
