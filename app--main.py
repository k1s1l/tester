"""Entry point: python -m app.main"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.base import BaseEventIsolation
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault
from aiohttp import web
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.database.fsm_storage import PostgresStorage
from app.database.session import SessionFactory, create_engine, create_session_factory
from app.handlers import build_root_router
from app.handlers.common import on_error
from app.logging_setup import setup_logging
from app.middlewares.throttling import ThrottlingMiddleware
from app.services.ai import ClaudeService
from app.services.assistant import SalesAssistant
from app.services.channel_import import ChannelImporter
from app.services.maintenance import BackgroundWorker
from app.services.notifications import NotificationSender

log = logging.getLogger("app")

CLIENT_COMMANDS = [
    BotCommand(command="start", description="Почати спочатку"),
    BotCommand(command="my_orders", description="Мої замовлення"),
    BotCommand(command="help", description="Допомога"),
    BotCommand(command="forget", description="Видалити мої дані діалогу"),
]
ADMIN_COMMANDS = CLIENT_COMMANDS + [
    BotCommand(command="admin", description="Панель адміністратора"),
    BotCommand(command="orders", description="Замовлення"),
    BotCommand(command="order", description="Деталі замовлення"),
    BotCommand(command="products", description="Каталог"),
    BotCommand(command="stock", description="Залишки"),
    BotCommand(command="add_product", description="Додати товар"),
    BotCommand(command="edit_product", description="Змінити товар"),
    BotCommand(command="stats", description="Статистика"),
    BotCommand(command="export_orders", description="Експорт CSV"),
]


def build_dispatcher(
    settings: Settings,
    session_factory: SessionFactory,
    bot: Bot,
    ai: ClaudeService,
    *,
    events_isolation: BaseEventIsolation | None = None,
) -> tuple[Dispatcher, NotificationSender]:
    sender = NotificationSender(bot, session_factory, settings)
    assistant = SalesAssistant(settings, session_factory, ai)
    importer = ChannelImporter(bot, session_factory, settings, ai)
    dp = Dispatcher(
        storage=PostgresStorage(session_factory),
        # One update at a time per user: protects checkout from double clicks / parallel messages.
        events_isolation=events_isolation or SimpleEventIsolation(),
        settings=settings,
        session_factory=session_factory,
        assistant=assistant,
        sender=sender,
        importer=importer,
        ai=ai,
    )
    throttling = ThrottlingMiddleware(settings)
    dp.message.outer_middleware(throttling)
    dp.callback_query.outer_middleware(throttling)
    dp.errors.register(on_error)
    dp.include_router(build_root_router())
    return dp, sender


async def setup_commands(bot: Bot, settings: Settings) -> None:
    await bot.set_my_commands(CLIENT_COMMANDS, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception as exc:  # admin has not started the bot yet
            log.warning("Cannot set admin commands for %s: %s", admin_id, type(exc).__name__)


async def retry_forever[T](name: str, call: Callable[[], Awaitable[T]]) -> T:
    """Retry a startup call (DB / Telegram may be briefly unavailable during deploys) instead of crashing.

    A wrong BOT_TOKEN is not retried: the process exits with a clear message.
    """
    delay = 2.0
    while True:
        try:
            return await call()
        except TelegramUnauthorizedError as exc:
            raise SystemExit("Telegram rejected BOT_TOKEN (401 Unauthorized). Check the token from @BotFather.") from exc
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter, OSError, SQLAlchemyError) as exc:
            log.warning("%s failed (%s), retrying in %.0fs", name, type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    if settings.bot_token is None:
        raise SystemExit("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    engine = create_engine(settings)

    async def check_db() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    await retry_forever("Database connection", check_db)
    session_factory = create_session_factory(engine)

    session = None
    if settings.telegram_api_url:
        session = AiohttpSession(api=TelegramAPIServer.from_base(settings.telegram_api_url))
    bot = Bot(
        settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    ai = ClaudeService(settings)
    if not ai.enabled:
        log.warning("ANTHROPIC_API_KEY is not set: the bot runs in rule-based mode without photo recognition")
    dp, sender = build_dispatcher(settings, session_factory, bot, ai)
    worker = BackgroundWorker(session_factory, settings, sender)

    async def on_startup() -> None:
        me = await retry_forever("Telegram getMe", bot.get_me)
        try:
            await setup_commands(bot, settings)
        except TelegramAPIError as exc:  # menu commands are cosmetic, never block the start
            log.warning("Could not set bot commands: %s", type(exc).__name__)
        worker.start()
        log.info("Bot @%s started in %s mode (model %s)", me.username, settings.bot_mode, settings.claude_model)

    async def on_shutdown() -> None:
        log.info("Shutting down...")
        try:  # let posts that are being imported finish
            await asyncio.wait_for(dp["importer"].wait_idle(), timeout=15)
        except TimeoutError:
            log.warning("Channel import still running at shutdown")
        await worker.stop()
        await ai.close()
        await engine.dispose()

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        if settings.bot_mode == "webhook":
            await _run_webhook(dp, bot, settings)
        else:
            await retry_forever("Telegram deleteWebhook", lambda: bot.delete_webhook(drop_pending_updates=False))
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        await bot.session.close()


async def _run_webhook(dp: Dispatcher, bot: Bot, settings: Settings) -> None:
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    if not settings.webhook_url or settings.webhook_secret is None:
        raise RuntimeError("BOT_MODE=webhook requires WEBHOOK_BASE_URL and WEBHOOK_SECRET")
    secret = settings.webhook_secret.get_secret_value()

    async def health(_: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def set_webhook() -> None:
        await retry_forever(
            "Telegram setWebhook",
            lambda: bot.set_webhook(
                settings.webhook_url,
                secret_token=secret,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False,
            ),
        )

    dp.startup.register(set_webhook)
    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path=settings.webhook_path)
    setup_application(app, dp, bot=bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=settings.host, port=settings.port)
    await site.start()
    log.info("Webhook server listening on %s:%s%s", settings.host, settings.port, settings.webhook_path)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await stop.wait()
    await runner.cleanup()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
