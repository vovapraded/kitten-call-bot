"""Persistent rolling windows. Store message IDs/times, never conversation text."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_COUNT,
    DEFAULT_MESSAGE,
    DEFAULT_WINDOW,
    MAX_COUNT,
    MAX_WINDOW,
    validate_message,
)


@dataclass(frozen=True)
class Settings:
    chat_id: int
    count: int = DEFAULT_COUNT
    window: int = DEFAULT_WINDOW
    message: str = DEFAULT_MESSAGE
    enabled: bool = True
    retry_at: float = 0
    last_message_id: int = 0
    last_photo: str | None = None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS chats (
                chat_id INTEGER PRIMARY KEY,
                count INTEGER NOT NULL,
                window INTEGER NOT NULL,
                message TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                retry_at REAL NOT NULL DEFAULT 0,
                last_message_id INTEGER NOT NULL DEFAULT 0,
                last_photo TEXT
            );
            CREATE TABLE IF NOT EXISTS activity (
                chat_id INTEGER NOT NULL REFERENCES chats(chat_id) ON DELETE CASCADE,
                message_id INTEGER NOT NULL,
                sent_at REAL NOT NULL,
                PRIMARY KEY (chat_id, message_id)
            );
            CREATE INDEX IF NOT EXISTS activity_time ON activity(chat_id, sent_at);
        """)

    def close(self) -> None:
        self.db.close()

    def settings(self, chat_id: int) -> Settings:
        row = self.db.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()
        if row is None:
            with self.db:
                self.db.execute(
                    "INSERT INTO chats(chat_id, count, window, message) VALUES(?, ?, ?, ?)",
                    (chat_id, DEFAULT_COUNT, DEFAULT_WINDOW, DEFAULT_MESSAGE),
                )
            return Settings(chat_id)
        values = dict(row)
        values["enabled"] = bool(values["enabled"])
        return Settings(**values)

    def configure(self, chat_id: int, **changes) -> Settings:
        allowed = {"count", "window", "message", "enabled"}
        if not changes or not changes.keys() <= allowed:
            raise ValueError("Неизвестные или пустые настройки.")
        if "count" in changes and (
            type(changes["count"]) is not int or not 1 <= changes["count"] <= MAX_COUNT
        ):
            raise ValueError(f"Количество сообщений: целое число от 1 до {MAX_COUNT}.")
        if "window" in changes and (
            type(changes["window"]) is not int or not 1 <= changes["window"] <= MAX_WINDOW
        ):
            raise ValueError(f"Окно: целое число от 1 до {MAX_WINDOW} минут.")
        if "message" in changes:
            changes["message"] = validate_message(changes["message"])
        if "enabled" in changes and type(changes["enabled"]) is not bool:
            raise ValueError("enabled должен быть bool.")
        self.settings(chat_id)
        assignments = ", ".join(f"{key}=?" for key in changes)
        with self.db:
            self.db.execute(
                f"UPDATE chats SET {assignments}, retry_at=0 WHERE chat_id=?",
                (*changes.values(), chat_id),
            )
            self.db.execute("DELETE FROM activity WHERE chat_id=?", (chat_id,))
        return self.settings(chat_id)

    def reset(self, chat_id: int) -> Settings:
        return self.configure(
            chat_id,
            count=DEFAULT_COUNT,
            window=DEFAULT_WINDOW,
            message=DEFAULT_MESSAGE,
            enabled=True,
        )

    def count_recent(self, chat_id: int, now: float) -> int:
        settings = self.settings(chat_id)
        with self.db:
            self.db.execute(
                "DELETE FROM activity WHERE chat_id=? AND sent_at<=?",
                (chat_id, now - settings.window * 60),
            )
        return self.db.execute(
            "SELECT COUNT(*) FROM activity WHERE chat_id=?",
            (chat_id,),
        ).fetchone()[0]

    def record(self, chat_id: int, message_id: int, sent_at: float, now: float) -> bool:
        """Return whether this new message crosses the threshold and may send.

        PTB processes updates sequentially. Telegram message IDs increase per chat;
        the high-water mark also deduplicates messages after a successful send/restart.
        The window is (now - X minutes, now]. Keep at most N newest timestamps.
        """
        settings = self.settings(chat_id)
        if message_id <= settings.last_message_id:
            return False
        with self.db:
            self.db.execute(
                "UPDATE chats SET last_message_id=? WHERE chat_id=?",
                (message_id, chat_id),
            )
            self.db.execute(
                "DELETE FROM activity WHERE chat_id=? AND sent_at<=?",
                (chat_id, now - settings.window * 60),
            )
            if not settings.enabled or sent_at <= now - settings.window * 60:
                return False
            self.db.execute(
                "INSERT INTO activity VALUES(?, ?, ?)",
                (chat_id, message_id, min(sent_at, now)),
            )
            self.db.execute(
                """DELETE FROM activity WHERE chat_id=? AND message_id IN (
                    SELECT message_id FROM activity WHERE chat_id=?
                    ORDER BY sent_at DESC, message_id DESC LIMIT -1 OFFSET ?
                )""",
                (chat_id, chat_id, settings.count),
            )
            count = self.db.execute(
                "SELECT COUNT(*) FROM activity WHERE chat_id=?",
                (chat_id,),
            ).fetchone()[0]
        return count >= settings.count and now >= settings.retry_at

    def sent(self, chat_id: int, photo: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM activity WHERE chat_id=?", (chat_id,))
            self.db.execute(
                "UPDATE chats SET retry_at=0, last_photo=? WHERE chat_id=?",
                (photo, chat_id),
            )

    def defer(self, chat_id: int, retry_at: float) -> None:
        with self.db:
            self.db.execute("UPDATE chats SET retry_at=? WHERE chat_id=?", (retry_at, chat_id))

    def remember_photo(self, chat_id: int, photo: str) -> None:
        with self.db:
            self.db.execute("UPDATE chats SET last_photo=? WHERE chat_id=?", (photo, chat_id))

    def migrate(self, old_chat_id: int, new_chat_id: int) -> None:
        """Move settings once. Telegram delivers migration in both chats."""
        old = self.db.execute(
            "SELECT * FROM chats WHERE chat_id=?",
            (old_chat_id,),
        ).fetchone()
        if old is None or old_chat_id == new_chat_id:
            return
        with self.db:
            self.db.execute("DELETE FROM chats WHERE chat_id=?", (new_chat_id,))
            self.db.execute("DELETE FROM activity WHERE chat_id=?", (old_chat_id,))
            self.db.execute(
                "UPDATE chats SET chat_id=?, last_message_id=0, retry_at=0 WHERE chat_id=?",
                (new_chat_id, old_chat_id),
            )

    def forget(self, chat_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM chats WHERE chat_id=?", (chat_id,))
