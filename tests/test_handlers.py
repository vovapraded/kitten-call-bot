"""Exercise Telegram handlers with real updates and an entirely offline bot."""

from base64 import b64decode
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update
from telegram.error import BadRequest, RetryAfter, TelegramError

from kitten_bot.app import Handlers, build_application
from kitten_bot.config import DEFAULT_MESSAGE, Config
from kitten_bot.photos import Photos
from kitten_bot.storage import Store

CHAT_ID = -100123
NOW = 1_700_000_000
PNG = b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jM4c"
    "AAAAASUVORK5CYII="
)


@pytest.fixture
def bot():
    return SimpleNamespace(
        id=999,
        username="KittenTestBot",
        get_chat_member=AsyncMock(return_value=SimpleNamespace(status="administrator")),
        send_message=AsyncMock(),
        send_photo=AsyncMock(),
    )


@pytest.fixture
def context(bot):
    return SimpleNamespace(bot=bot, args=[])


@pytest.fixture
def store(tmp_path):
    instance = Store(tmp_path / "bot.sqlite3")
    yield instance
    instance.close()


@pytest.fixture
def photos(tmp_path):
    directory = tmp_path / "kittens"
    directory.mkdir()
    (directory / "kitten-one.png").write_bytes(PNG)
    (directory / "kitten-two.png").write_bytes(PNG)
    return Photos(directory)


@pytest.fixture
def handlers(store, photos, monkeypatch):
    monkeypatch.setattr("kitten_bot.app.time.time", lambda: NOW)
    return Handlers(store, photos)


@pytest.fixture
def make_update(bot):
    def factory(
        text="Hello",
        *,
        message_id=1,
        chat_type="supergroup",
        chat_id=CHAT_ID,
        update_type="message",
        **fields,
    ):
        message = {
            "message_id": message_id,
            "date": NOW,
            "chat": {"id": chat_id, "type": chat_type, "title": "Test group"},
            "from": {"id": 101, "is_bot": False, "first_name": "Test user"},
        }
        if text is not None:
            message["text"] = text
            if text.startswith("/"):
                message["entities"] = [
                    {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
                ]
        message.update(fields)
        update = Update.de_json({"update_id": message_id, update_type: message}, bot)
        update.effective_message.set_bot(bot)
        return update

    return factory


@pytest.mark.parametrize("status", ["member", "restricted", "left", "kicked"])
async def test_non_admin_cannot_change_settings(handlers, store, bot, context, make_update, status):
    bot.get_chat_member.return_value = SimpleNamespace(status=status)
    context.args = ["7"]

    await handlers.setcount(make_update("/setcount 7"), context)

    assert store.settings(CHAT_ID).count == 20
    bot.get_chat_member.assert_awaited_once_with(CHAT_ID, 101)
    assert "только администраторы" in bot.send_message.call_args.kwargs["text"]


async def test_private_command_rejected_before_membership_check(
    handlers, bot, context, make_update,
):
    context.args = ["7"]
    await handlers.setcount(make_update("/setcount 7", chat_type="private", chat_id=101), context)

    bot.get_chat_member.assert_not_awaited()
    assert "в группе" in bot.send_message.call_args.kwargs["text"]


async def test_channel_sender_cannot_change_settings(
    handlers, store, bot, context, make_update,
):
    context.args = ["7"]
    update = make_update(
        "/setcount 7", sender_chat={"id": -100456, "type": "channel", "title": "Channel"},
    )

    await handlers.setcount(update, context)

    assert store.settings(CHAT_ID).count == 20
    bot.get_chat_member.assert_not_awaited()
    assert "Отправьте команду от своего имени" in bot.send_message.call_args.kwargs["text"]


async def test_anonymous_group_admin_can_change_settings(
    handlers, store, bot, context, make_update,
):
    context.args = ["7"]
    update = make_update(
        "/setcount 7",
        sender_chat={"id": CHAT_ID, "type": "supergroup", "title": "Test group"},
        **{"from": {"id": 1087968824, "is_bot": True, "first_name": "GroupAnonymousBot"}},
    )

    await handlers.setcount(update, context)

    assert store.settings(CHAT_ID).count == 7
    bot.get_chat_member.assert_not_awaited()


async def test_admin_lookup_failure_does_not_authorize_command(
    handlers, store, bot, context, make_update,
):
    bot.get_chat_member.side_effect = TelegramError("unavailable")
    context.args = ["7"]

    await handlers.setcount(make_update("/setcount 7"), context)

    assert store.settings(CHAT_ID).count == 20
    assert "Не удалось проверить права" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.parametrize("window", [40, 15, 1440])
async def test_setwindow_persists_and_clears_activity(
    handlers, store, context, make_update, window,
):
    store.record(CHAT_ID, 1, NOW, NOW)
    context.args = [str(window)]

    await handlers.setwindow(make_update(f"/setwindow {window}", message_id=2), context)

    assert store.settings(CHAT_ID).window == window
    assert store.count_recent(CHAT_ID, NOW) == 0


@pytest.mark.parametrize("args", [[], ["0"], ["1441"], ["1.5"], ["no"], ["3", "4"]])
async def test_invalid_window_does_not_change_settings_or_counter(
    handlers, store, bot, context, make_update, args,
):
    store.configure(CHAT_ID, window=15)
    store.record(CHAT_ID, 1, NOW, NOW)
    context.args = args

    await handlers.setwindow(make_update("/setwindow", message_id=2), context)

    assert store.settings(CHAT_ID).window == 15
    assert store.count_recent(CHAT_ID, NOW) == 1
    assert "Используйте /setwindow" in bot.send_message.call_args.kwargs["text"]


@pytest.mark.parametrize("command", ["/setmessage", "/setmessage@KittenTestBot"])
async def test_setmessage_preserves_multiline_text(
    handlers, store, context, make_update, command,
):
    caption = "Котята,  пора созвониться?\nВторая строка 🐱"
    context.args = caption.split()

    await handlers.setmessage(make_update(f"{command} {caption}"), context)

    assert store.settings(CHAT_ID).message == caption


@pytest.mark.parametrize("caption", ["", "🐱" * 513])
async def test_invalid_caption_preserves_previous_setting(
    handlers, store, context, make_update, caption,
):
    await handlers.setmessage(make_update(f"/setmessage {caption}"), context)

    assert store.settings(CHAT_ID).message == DEFAULT_MESSAGE


async def test_test_command_sends_photo_without_resetting_activity(
    handlers, store, bot, context, make_update,
):
    store.record(CHAT_ID, 1, NOW, NOW)

    await handlers.test(make_update("/test", message_id=2), context)

    bot.send_photo.assert_awaited_once()
    assert bot.send_photo.call_args.kwargs["caption"] == DEFAULT_MESSAGE
    assert store.count_recent(CHAT_ID, NOW) == 1
    assert store.settings(CHAT_ID).last_photo in {"kitten-one.png", "kitten-two.png"}


async def test_threshold_sends_photo_and_caption_to_triggering_topic(
    handlers, store, bot, context, make_update,
):
    caption = "Котята, созвонимся?"
    store.configure(CHAT_ID, count=2, message=caption)
    uploaded = []

    async def capture_photo(**kwargs):
        uploaded.append(kwargs["photo"].read())

    bot.send_photo.side_effect = capture_photo
    await handlers.message(
        make_update(message_id=1, is_topic_message=True, message_thread_id=9), context,
    )
    bot.send_photo.assert_not_awaited()

    await handlers.message(
        make_update(message_id=2, is_topic_message=True, message_thread_id=17), context,
    )

    bot.send_photo.assert_awaited_once()
    sent = bot.send_photo.call_args.kwargs
    assert sent["chat_id"] == CHAT_ID
    assert sent["caption"] == caption
    assert sent["message_thread_id"] == 17
    assert uploaded == [PNG]
    assert store.count_recent(CHAT_ID, NOW) == 0


async def test_send_failure_retains_activity_and_obeys_backoff(
    handlers, store, bot, context, make_update, monkeypatch,
):
    store.configure(CHAT_ID, count=2)
    bot.send_photo.side_effect = BadRequest("photo denied")
    await handlers.message(make_update(message_id=1), context)
    await handlers.message(make_update(message_id=2), context)

    assert store.count_recent(CHAT_ID, NOW) == 2
    assert store.settings(CHAT_ID).retry_at == NOW + 30
    assert bot.send_photo.await_count == 1

    monkeypatch.setattr("kitten_bot.app.time.time", lambda: NOW + 29)
    await handlers.message(make_update(message_id=3, date=NOW + 29), context)
    assert bot.send_photo.await_count == 1
    assert store.count_recent(CHAT_ID, NOW + 29) == 2

    bot.send_photo.side_effect = None
    monkeypatch.setattr("kitten_bot.app.time.time", lambda: NOW + 30)
    await handlers.message(make_update(message_id=4, date=NOW + 30), context)
    assert bot.send_photo.await_count == 2
    assert store.count_recent(CHAT_ID, NOW + 30) == 0
    assert store.settings(CHAT_ID).retry_at == 0


@pytest.mark.parametrize("delay", [90, timedelta(seconds=90)])
@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")
async def test_retry_after_honors_telegram_delay(
    handlers, store, bot, context, make_update, monkeypatch, delay,
):
    monkeypatch.setenv("PTB_TIMEDELTA", "1" if isinstance(delay, timedelta) else "0")
    store.configure(CHAT_ID, count=1)
    bot.send_photo.side_effect = RetryAfter(delay)

    await handlers.message(make_update(), context)

    assert store.settings(CHAT_ID).retry_at == NOW + 90
    assert store.count_recent(CHAT_ID, NOW) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"from": {"id": 555, "is_bot": True, "first_name": "Another bot"}},
        {"sender_chat": {"id": -100456, "type": "channel", "title": "Channel"}},
    ],
)
async def test_bot_and_channel_messages_are_not_counted(
    handlers, store, bot, context, make_update, fields,
):
    store.configure(CHAT_ID, count=1)

    await handlers.message(make_update(**fields), context)

    bot.send_photo.assert_not_awaited()
    assert store.count_recent(CHAT_ID, NOW) == 0


async def test_anonymous_admin_message_counts_as_conversation(
    handlers, store, context, make_update,
):
    update = make_update(
        sender_chat={"id": CHAT_ID, "type": "supergroup", "title": "Test group"},
        **{"from": {"id": 1087968824, "is_bot": True, "first_name": "GroupAnonymousBot"}},
    )

    await handlers.message(update, context)

    assert store.count_recent(CHAT_ID, NOW) == 1


@pytest.mark.parametrize(
    ("text", "fields", "expected"),
    [
        ("Hi", {}, "message"),
        (None, {"sticker": {"file_id": "x", "file_unique_id": "y", "type": "regular",
                            "width": 1, "height": 1, "is_animated": False,
                            "is_video": False}}, "message"),
        (None, {"photo": [{"file_id": "x", "file_unique_id": "y", "width": 1,
                           "height": 1}]}, "message"),
        (None, {"voice": {"file_id": "x", "file_unique_id": "y", "duration": 1}}, "message"),
        ("/settings", {}, "settings"),
        ("/setwindow@KittenTestBot 40", {}, "setwindow"),
        ("/setmessage@KittenTestBot First\nSecond", {}, "setmessage"),
        ("/setcount@AnotherBot 1", {}, None),
        ("/unknown", {}, None),
        ("Hi", {"update_type": "edited_message"}, None),
        ("/setcount 1", {"update_type": "edited_message"}, None),
        ("Hi", {"update_type": "channel_post", "chat_type": "channel"}, None),
        ("Hi", {"chat_type": "private", "chat_id": 101}, None),
        (None, {"new_chat_title": "Renamed"}, None),
        (None, {"migrate_to_chat_id": -100789}, "migrate"),
    ],
)
def test_registered_filters_route_updates_without_network(
    store, photos, bot, make_update, text, fields, expected,
):
    application = build_application(Config(token="123456:OFFLINE_TEST_TOKEN"), store, photos)
    update = make_update(text, **fields)
    matching = [
        handler.callback.__name__
        for handler in application.handlers[0]
        if handler.check_update(update)
    ]

    assert matching == ([] if expected is None else [expected])
    bot.send_message.assert_not_awaited()
    bot.send_photo.assert_not_awaited()
    bot.get_chat_member.assert_not_awaited()
