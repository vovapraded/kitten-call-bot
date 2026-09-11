"""Telegram handlers. All chat state mutations run sequentially."""

import logging
import time
from datetime import timedelta

from telegram import BotCommand, Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import MAX_COUNT, MAX_WINDOW, Config
from .network import ReliableBot
from .photos import Photos
from .storage import Settings, Store

logger = logging.getLogger(__name__)
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}
HELP = """Котеночки, может, созвонимся? 🐾

Когда в группе наберётся N сообщений за последние X минут, пришлю котёнка
с напоминанием. По умолчанию — 20 сообщений за 40 минут.
После отправки отсчёт начинается заново.

/settings — настройки и текущий счётчик
/help — эта справка

Команды администраторов (в группе):
/setcount 20 — сколько сообщений (1–10000)
/setwindow 40 — окно в минутах (1–1440)
/setmessage Ваш текст — подпись к фото (до 1024 символов)
/pause — приостановить напоминания
/resume — возобновить
/reset — вернуть 20 / 40 и исходный текст, включить бота
/test — прислать пробного котёнка

Изменение настроек сбрасывает счётчик. /test его не меняет.
Считаю новые сообщения людей, включая фото, стикеры и голосовые.
Команды, правки и служебные сообщения не считаю.
В группе с темами счётчик общий, отвечаю в тему последнего сообщения.

Чтобы видеть переписку: @BotFather → /setprivacy → Disable,
затем удалите и снова добавьте меня в группу. Назначьте администратором
с минимальными правами, чтобы я могла проверять права на команды.
Нужно разрешение на отправку сообщений и фотографий."""


def format_settings(settings: Settings, count: int) -> str:
    state = "включён" if settings.enabled else "на паузе"
    return (
        f"Бот {state} 🐾\n"
        f"Порог: {settings.count} сообщений\n"
        f"Окно: {settings.window} минут\n"
        f"Сейчас в окне: {count} / {settings.count}\n\n"
        f"Сообщение:\n{settings.message}"
    )


class Handlers:
    def __init__(self, store: Store, photos: Photos):
        self.store = store
        self.photos = photos

    async def admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        chat, message, user = update.effective_chat, update.effective_message, update.effective_user
        if not chat or not message:
            return False
        if chat.type not in GROUP_TYPES:
            await message.reply_text("Эту команду нужно отправить в группе, где я добавлена.")
            return False
        # Telegram identifies anonymous administrators by the group itself as sender_chat.
        if message.sender_chat is not None:
            if message.sender_chat.id == chat.id:
                return True
            await message.reply_text("Отправьте команду от своего имени или от имени этой группы.")
            return False
        if user is None or user.is_bot:
            return False
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except TelegramError:
            await message.reply_text(
                "Не удалось проверить права. Назначьте меня администратором группы "
                "с минимальными правами и повторите команду."
            )
            return False
        if member.status not in {ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR}:
            await message.reply_text(
                "Менять настройки и запускать тест могут только администраторы."
            )
            return False
        return True

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(HELP)

    async def settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat, message = update.effective_chat, update.effective_message
        if not chat or not message:
            return
        if chat.type not in GROUP_TYPES:
            await message.reply_text(
                "Настройки отдельные для каждой группы. Отправьте /settings там."
            )
            return
        settings = self.store.settings(chat.id)
        await message.reply_text(
            format_settings(settings, self.store.count_recent(chat.id, time.time()))
        )

    async def set_number(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, key: str
    ) -> None:
        if not await self.admin(update, context):
            return
        command, maximum = ("setcount", MAX_COUNT) if key == "count" else ("setwindow", MAX_WINDOW)
        try:
            if len(context.args) != 1:
                raise ValueError
            value = int(context.args[0])
            if not 1 <= value <= maximum:
                raise ValueError
        except ValueError:
            await update.effective_message.reply_text(
                f"Используйте /{command} <целое число от 1 до {maximum}>."
            )
            return
        settings = self.store.configure(update.effective_chat.id, **{key: value})
        await update.effective_message.reply_text(
            "Сохранено, отсчёт начинается заново.\n\n" + format_settings(settings, 0)
        )

    async def setcount(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.set_number(update, context, "count")

    async def setwindow(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.set_number(update, context, "window")

    async def setmessage(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.admin(update, context):
            return
        # Preserve line breaks and internal whitespace instead of rejoining context.args.
        parts = (update.effective_message.text or "").split(maxsplit=1)
        try:
            text = parts[1] if len(parts) == 2 else ""
            settings = self.store.configure(update.effective_chat.id, message=text)
        except ValueError as exc:
            await update.effective_message.reply_text(
                f"{exc}\nПример: /setmessage Может, созвонимся?)"
            )
            return
        await update.effective_message.reply_text(
            "Текст сохранён, отсчёт начинается заново.\n\n" + format_settings(settings, 0)
        )

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.admin(update, context):
            self.store.configure(update.effective_chat.id, enabled=False)
            await update.effective_message.reply_text("Напоминания на паузе. Включить: /resume 🐾")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.admin(update, context):
            self.store.configure(update.effective_chat.id, enabled=True)
            await update.effective_message.reply_text("Снова считаю сообщения с нуля 🐾")

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.admin(update, context):
            settings = self.store.reset(update.effective_chat.id)
            await update.effective_message.reply_text(
                "Исходные настройки восстановлены.\n\n" + format_settings(settings, 0)
            )

    async def send_kitten(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *,
        test: bool = False,
    ) -> bool:
        chat, message = update.effective_chat, update.effective_message
        settings = self.store.settings(chat.id)
        if time.time() < settings.retry_at:
            if test:
                await message.reply_text(
                    "После ошибки отправки нужно немного подождать и повторить."
                )
            return False
        photo = self.photos.choose(settings.last_photo)
        try:
            with photo.open("rb") as picture:
                await context.bot.send_photo(
                    chat_id=chat.id,
                    photo=picture,
                    caption=settings.message,
                    message_thread_id=message.message_thread_id
                    if message.is_topic_message
                    else None,
                )
        except (TelegramError, OSError) as exc:
            delay = 30.0
            if isinstance(exc, RetryAfter):
                retry = exc.retry_after
                delay = max(delay, retry.total_seconds() if isinstance(retry, timedelta) else retry)
            self.store.defer(chat.id, time.time() + delay)
            # Exception strings/tracebacks can contain URLs with the Telegram token.
            logger.warning(
                "Kitten delivery failed (%s); retry on a later message", type(exc).__name__
            )
            if test and not isinstance(exc, RetryAfter):
                await message.reply_text(
                    "Не получилось отправить фото. Проверьте разрешение на отправку фотографий "
                    "и соединение с Telegram. Повторите /test через 30 секунд."
                )
            return False
        if test:
            self.store.remember_photo(chat.id, photo.name)
        else:
            self.store.sent(chat.id, photo.name)
        return True

    async def test(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if await self.admin(update, context):
            await self.send_kitten(update, context, test=True)

    async def message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Only new group messages; edits, commands and service events are filtered on registration.
        message = update.message
        if message is None or message.chat.type not in GROUP_TYPES:
            return
        if message.sender_chat:
            if message.sender_chat.id != message.chat.id:
                return  # Channel posts and automatic discussion forwards aren't conversation.
        elif message.from_user is None or message.from_user.is_bot:
            return
        if self.store.record(
            message.chat_id,
            message.message_id,
            message.date.timestamp(),
            time.time(),
        ):
            await self.send_kitten(update, context)

    async def migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.message
        if message.migrate_to_chat_id:
            self.store.migrate(message.chat_id, message.migrate_to_chat_id)
        elif message.migrate_from_chat_id:
            self.store.migrate(message.migrate_from_chat_id, message.chat_id)

    async def membership(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        if change is None or change.chat.type not in GROUP_TYPES:
            return
        old_status = change.old_chat_member.status
        status = change.new_chat_member.status
        absent = {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
        # Keep settings on removal: during group->supergroup migration a LEFT event
        # may precede migrate_to_chat_id. A later re-add should preserve configuration.
        if old_status in absent and status not in absent:
            self.store.settings(change.chat.id)
            await context.bot.send_message(
                chat_id=change.chat.id,
                text="Я на месте 🐾 Настройки: /settings, инструкция: /help. "
                "По умолчанию присылаю котёнка каждые 20 сообщений за последние 40 минут.",
            )

    async def error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Update failed (%s)", type(context.error).__name__)

    async def startup(self, application: Application) -> None:
        try:
            await application.bot.set_my_commands(
                [
                    BotCommand("settings", "Настройки и счётчик этой группы"),
                    BotCommand("setcount", "Количество сообщений: /setcount 20"),
                    BotCommand("setwindow", "Окно в минутах: /setwindow 40"),
                    BotCommand("setmessage", "Текст: /setmessage Может, созвонимся?)"),
                    BotCommand("pause", "Приостановить напоминания"),
                    BotCommand("resume", "Возобновить напоминания"),
                    BotCommand("reset", "Вернуть исходные настройки"),
                    BotCommand("test", "Прислать пробного котёнка"),
                    BotCommand("help", "Как пользоваться ботом"),
                ]
            )
        except TelegramError as exc:
            logger.warning("Could not register menu (%s)", type(exc).__name__)
        logger.info("Настройка меню завершена; подключаю получение сообщений")


def build_application(config: Config, store: Store, photos: Photos) -> Application:
    handlers = Handlers(store, photos)
    app = (
        Application.builder()
        .bot(ReliableBot(config.token, config.telegram_timeout))
        .concurrent_updates(False)
        .post_init(handlers.startup)
        .build()
    )
    # A single group ensures each update is processed by at most one handler.
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, handlers.migrate))
    for name in (
        "start",
        "help",
        "settings",
        "setcount",
        "setwindow",
        "setmessage",
        "pause",
        "resume",
        "reset",
        "test",
    ):
        callback = handlers.help if name == "start" else getattr(handlers, name)
        app.add_handler(CommandHandler(name, callback, filters=filters.UpdateType.MESSAGE))
    app.add_handler(
        MessageHandler(
            filters.UpdateType.MESSAGE
            & filters.ChatType.GROUPS
            & ~filters.COMMAND
            & ~filters.StatusUpdate.ALL,
            handlers.message,
        )
    )
    app.add_handler(ChatMemberHandler(handlers.membership, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(handlers.error)
    return app
