import pytest

from kitten_bot.config import DEFAULT_MESSAGE, MAX_COUNT, MAX_WINDOW
from kitten_bot.storage import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "nested" / "bot.sqlite3")
    yield instance
    instance.close()


def test_default_twentieth_message_within_forty_minutes_triggers(store):
    settings = store.settings(-100)
    assert (settings.count, settings.window, settings.enabled) == (20, 40, True)
    assert settings.message == DEFAULT_MESSAGE

    now = 10_000
    # The first message is 30 minutes old: a 20-minute default would fail this test.
    for message_id in range(1, 20):
        sent_at = now - 1800 + message_id
        assert not store.record(-100, message_id, sent_at, now)
    assert store.record(-100, 20, now, now)
    assert store.count_recent(-100, now) == 20


def test_window_excludes_exact_lower_boundary_and_includes_newer_messages(store):
    store.configure(-100, count=2)
    now = 10_000
    assert not store.record(-100, 1, now - 2400, now)
    assert store.count_recent(-100, now) == 0
    assert not store.record(-100, 2, now - 2400 + 0.001, now)
    assert store.record(-100, 3, now, now)
    assert store.count_recent(-100, now + 0.001) == 1


def test_only_successful_send_consumes_batch_and_next_batch_needs_n_messages(store):
    store.configure(-100, count=3)
    for message_id in (1, 2):
        assert not store.record(-100, message_id, message_id, message_id)
    assert store.record(-100, 3, 3, 3)
    assert store.count_recent(-100, 3) == 3

    # A failed request postpones sending; it must not erase counted messages.
    store.defer(-100, 60)
    assert store.count_recent(-100, 3) == 3
    assert not store.record(-100, 4, 10, 10)
    assert store.count_recent(-100, 10) == 3
    assert store.record(-100, 5, 60, 60)
    store.sent(-100, "blanket.jpg")
    assert store.count_recent(-100, 60) == 0
    assert store.settings(-100).retry_at == 0
    assert store.settings(-100).last_photo == "blanket.jpg"

    assert not store.record(-100, 6, 61, 61)
    assert not store.record(-100, 7, 62, 62)
    assert store.record(-100, 8, 63, 63)


def test_backoff_retains_n_newest_messages_and_expiry_requires_new_threshold(store):
    store.configure(-100, count=3, window=1)
    for message_id, timestamp in enumerate((0, 1, 2), 1):
        store.record(-100, message_id, timestamp, timestamp)
    store.defer(-100, 100)
    for message_id, timestamp in enumerate((30, 40, 50), 4):
        assert not store.record(-100, message_id, timestamp, timestamp)
        assert store.count_recent(-100, timestamp) == 3

    # The original 0/1/2-second batch has expired; the latest 30/40/50 batch survives.
    assert store.count_recent(-100, 61) == 3
    # Backoff ending does not resurrect expired activity (30 and 40 are now too old).
    assert not store.record(-100, 7, 100, 100)
    assert store.count_recent(-100, 100) == 2
    assert store.record(-100, 8, 101, 101)


def test_old_backlog_never_triggers_and_is_not_counted_twice(store):
    store.configure(-100, count=1)
    assert not store.record(-100, 1, 100, 5000)
    assert not store.record(-100, 2, 2600, 5000)
    assert store.count_recent(-100, 5000) == 0
    assert not store.record(-100, 1, 5000, 5000)
    assert store.record(-100, 3, 5000, 5000)


def test_future_timestamp_cannot_keep_activity_alive_beyond_window(store):
    store.configure(-100, count=2, window=1)
    assert not store.record(-100, 1, 999_999, 100)
    assert store.count_recent(-100, 159) == 1
    assert store.count_recent(-100, 160) == 0


@pytest.mark.parametrize("operation", ["send", "reset", "restart"])
def test_duplicate_and_older_messages_stay_ignored_after_state_changes(tmp_path, operation):
    path = tmp_path / "bot.sqlite3"
    store = Store(path)
    try:
        store.configure(-100, count=1)
        assert store.record(-100, 40, 100, 100)
        if operation == "send":
            store.sent(-100, "shelter.jpg")
        elif operation == "reset":
            store.reset(-100)
        else:
            store.close()
            store = Store(path)
        count = store.count_recent(-100, 100)
        assert not store.record(-100, 40, 100, 100)
        assert not store.record(-100, 39, 100, 100)
        assert store.count_recent(-100, 100) == count
        assert store.settings(-100).last_message_id == 40
    finally:
        store.close()


def test_settings_activity_retry_and_photo_survive_restart_and_are_isolated(tmp_path):
    path = tmp_path / "bot.sqlite3"
    store = Store(path)
    store.configure(-100, count=3, window=7, message="  Пора созвониться 🐱  ")
    store.configure(-200, count=2, window=8, message="Другой чат")
    store.record(-100, 1, 100, 100)
    store.record(-200, 1, 100, 100)
    store.defer(-100, 200)
    store.remember_photo(-100, "sleeping.jpg")
    store.close()

    reopened = Store(path)
    try:
        first = reopened.settings(-100)
        second = reopened.settings(-200)
        assert (first.count, first.window, first.message) == (3, 7, "Пора созвониться 🐱")
        assert (first.retry_at, first.last_photo, first.last_message_id) == (
            200,
            "sleeping.jpg",
            1,
        )
        assert (second.count, second.window, second.message) == (2, 8, "Другой чат")
        assert (second.retry_at, second.last_photo) == (0, None)
        assert reopened.count_recent(-100, 100) == 1
        assert reopened.count_recent(-200, 100) == 1
        reopened.sent(-100, "newborn.jpg")
        assert reopened.count_recent(-200, 100) == 1
        assert reopened.settings(-200).last_photo is None
    finally:
        reopened.close()


def test_pause_and_resume_start_fresh_without_recounting_paused_messages(store):
    store.configure(-100, count=2)
    store.record(-100, 1, 100, 100)
    store.configure(-100, enabled=False)
    assert store.count_recent(-100, 100) == 0
    assert not store.record(-100, 2, 101, 101)
    assert not store.record(-100, 3, 102, 102)
    assert store.count_recent(-100, 102) == 0
    assert not store.settings(-100).enabled

    store.configure(-100, enabled=True)
    assert not store.record(-100, 3, 102, 103)
    assert not store.record(-100, 4, 103, 103)
    assert store.record(-100, 5, 104, 104)


def test_reset_restores_twenty_messages_forty_minutes_and_clears_pending_activity(store):
    store.configure(-100, count=2, window=1, message="Настроенный текст")
    store.record(-100, 1, 100, 100)
    store.defer(-100, 200)
    store.configure(-100, enabled=False)
    settings = store.reset(-100)
    assert (settings.count, settings.window, settings.message, settings.enabled) == (
        20,
        40,
        DEFAULT_MESSAGE,
        True,
    )
    assert settings.retry_at == 0
    assert store.count_recent(-100, 100) == 0
    assert not store.record(-100, 1, 100, 100)


def test_reconfiguration_clears_only_its_chat_activity_and_backoff(store):
    for chat_id in (-100, -200):
        store.record(chat_id, 1, 100, 100)
        store.defer(chat_id, 200)
    store.configure(-100, window=5)
    assert store.count_recent(-100, 100) == 0
    assert store.settings(-100).retry_at == 0
    assert store.count_recent(-200, 100) == 1
    assert store.settings(-200).retry_at == 200


def test_group_migration_preserves_settings_and_is_idempotent(store):
    old_chat, new_chat = -123, -100123
    store.configure(old_chat, count=2, window=7, message="Созвон?", enabled=False)
    store.remember_photo(old_chat, "blanket.jpg")
    store.record(old_chat, 900, 100, 100)
    store.defer(old_chat, 200)
    # Telegram may have delivered an update in the new chat before migration.
    store.configure(new_chat, count=1)
    store.record(new_chat, 10, 100, 100)
    store.migrate(old_chat, new_chat)

    settings = store.settings(new_chat)
    assert (settings.count, settings.window, settings.message, settings.enabled) == (
        2,
        7,
        "Созвон?",
        False,
    )
    assert settings.last_photo == "blanket.jpg"
    assert (settings.last_message_id, settings.retry_at) == (0, 0)
    assert store.count_recent(new_chat, 100) == 0
    store.configure(new_chat, enabled=True)
    assert not store.record(new_chat, 1, 100, 100)
    store.migrate(old_chat, new_chat)
    assert store.settings(new_chat).enabled
    assert store.count_recent(new_chat, 100) == 1
    assert store.record(new_chat, 2, 101, 101)


def test_same_chat_and_unknown_source_migrations_do_nothing(store):
    store.configure(-100, count=2)
    store.record(-100, 1, 100, 100)
    store.migrate(-100, -100)
    store.migrate(-999, -100)
    assert store.settings(-100).count == 2
    assert store.count_recent(-100, 100) == 1


def test_forget_removes_settings_and_activity_without_affecting_other_chats(store):
    for chat_id in (-100, -200):
        store.configure(chat_id, count=2)
        store.record(chat_id, 1, 100, 100)
    store.forget(-100)
    store.forget(-100)
    assert store.settings(-100).count == 20
    assert store.count_recent(-100, 100) == 0
    assert store.settings(-200).count == 2
    assert store.count_recent(-200, 100) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"unknown": 1},
        {"last_message_id": 50},
        {"count": 0},
        {"count": MAX_COUNT + 1},
        {"count": True},
        {"count": "20"},
        {"count": 2.5},
        {"window": 0},
        {"window": MAX_WINDOW + 1},
        {"window": False},
        {"window": "40"},
        {"window": 1.5},
        {"enabled": 1},
        {"enabled": "false"},
        {"message": "   "},
        {"message": "🐱" * 513},
    ],
)
def test_invalid_settings_do_not_change_existing_settings_or_activity(store, changes):
    before = store.configure(-100, count=3, window=7)
    store.record(-100, 1, 100, 100)
    with pytest.raises(ValueError):
        store.configure(-100, **changes)
    after = store.settings(-100)
    assert (after.count, after.window, after.message, after.enabled) == (
        before.count,
        before.window,
        before.message,
        before.enabled,
    )
    assert store.count_recent(-100, 100) == 1


@pytest.mark.parametrize("count, window", [(1, 1), (MAX_COUNT, MAX_WINDOW)])
def test_inclusive_configuration_limits_are_accepted(store, count, window):
    settings = store.configure(-100, count=count, window=window)
    assert (settings.count, settings.window) == (count, window)
