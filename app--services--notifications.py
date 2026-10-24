"""Transactional outbox for Telegram notifications.

``enqueue*`` functions only insert rows (inside the business transaction). ``NotificationSender``
delivers due rows with retries and exponential backoff. If Telegram is unavailable the order is
already safely committed and the message is retried later; every failure is recorded in
``notifications.last_error`` and logged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Notification, NotificationStatus, Order
from app.database.session import SessionFactory
from app.keyboards.admin import order_actions_keyboard
from app.keyboards.client import client_order_keyboard
from app.services.inventory import utcnow
from app.services.order_rules import CLIENT_CANCELLABLE

log = logging.getLogger(__name__)

KEYBOARD_ADMIN_ORDER = "admin_order"
KEYBOARD_CLIENT_ORDER = "client_order"


async def enqueue(
    session: AsyncSession,
    *,
    chat_id: int,
    text: str,
    kind: str,
    order_id: int | None = None,
    keyboard: str | None = None,
) -> Notification:
    notification = Notification(
        chat_id=chat_id,
        text=text,
        kind=kind,
        order_id=order_id,
        keyboard=keyboard,
        status=NotificationStatus.PENDING,
        next_attempt_at=utcnow(),
    )
    session.add(notification)
    await session.flush()
    return notification


async def enqueue_admins(
    session: AsyncSession,
    settings: Settings,
    *,
    text: str,
    kind: str,
    order_id: int | None = None,
    keyboard: str | None = None,
) -> list[Notification]:
    chats = settings.admin_notification_chats
    if not chats:
        log.warning("No ADMIN_CHAT_ID / ADMIN_IDS configured: admin notification '%s' is not queued", kind)
    return [
        await enqueue(session, chat_id=chat, text=text, kind=kind, order_id=order_id, keyboard=keyboard)
        for chat in chats
    ]


def backoff(attempts: int) -> timedelta:
    """15s, 30s, 1m, 2m, 4m ... capped at 1 hour."""
    return timedelta(seconds=min(15 * 2 ** max(attempts - 1, 0), 3600))


class NotificationSender:
    def __init__(self, bot: Any, session_factory: SessionFactory, settings: Settings) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._settings = settings
        self._lock = asyncio.Lock()

    async def deliver_due(self, limit: int = 25) -> int:
        """Send all due notifications. Safe to call concurrently / from several processes."""
        async with self._lock:
            sent = 0
            async with self._session_factory() as session, session.begin():
                rows = (
                    await session.scalars(
                        select(Notification)
                        .where(
                            Notification.status == NotificationStatus.PENDING,
                            Notification.next_attempt_at <= utcnow(),
                        )
                        .order_by(Notification.id)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                for notification in rows:
                    if await self._send_one(session, notification):
                        sent += 1
            return sent

    async def _markup(self, session: AsyncSession, notification: Notification) -> InlineKeyboardMarkup | None:
        if notification.order_id is None or notification.keyboard is None:
            return None
        order = await session.get(Order, notification.order_id)
        if order is None:
            return None
        if notification.keyboard == KEYBOARD_ADMIN_ORDER:
            return order_actions_keyboard(order.id, order.status)
        if notification.keyboard == KEYBOARD_CLIENT_ORDER and order.status in CLIENT_CANCELLABLE:
            return client_order_keyboard(order.id)
        return None

    async def _send_one(self, session: AsyncSession, notification: Notification) -> bool:
        notification.attempts += 1
        try:
            markup = await self._markup(session, notification)
            message = await self._bot.send_message(
                chat_id=notification.chat_id, text=notification.text, reply_markup=markup
            )
        except TelegramRetryAfter as exc:
            notification.next_attempt_at = utcnow() + timedelta(seconds=exc.retry_after + 1)
            notification.last_error = f"RetryAfter {exc.retry_after}s"
            log.warning("Notification %s rate limited, retry in %ss", notification.id, exc.retry_after)
            return False
        except (TelegramForbiddenError, TelegramNotFound, TelegramUnauthorizedError, TelegramBadRequest) as exc:
            # Permanent errors: bot blocked by the user, wrong chat id, bad markup...
            notification.status = NotificationStatus.FAILED
            notification.last_error = f"{type(exc).__name__}: {exc}"[:512]
            log.error(
                "Notification %s to chat %s failed permanently: %s",
                notification.id,
                notification.chat_id,
                notification.last_error,
            )
            return False
        except (TimeoutError, TelegramAPIError, OSError) as exc:
            notification.last_error = f"{type(exc).__name__}: {exc}"[:512]
            if notification.attempts >= self._settings.notification_max_attempts:
                notification.status = NotificationStatus.FAILED
                log.error("Notification %s gave up after %s attempts", notification.id, notification.attempts)
            else:
                notification.next_attempt_at = utcnow() + backoff(notification.attempts)
                log.warning(
                    "Notification %s failed (attempt %s), will retry: %s",
                    notification.id,
                    notification.attempts,
                    notification.last_error,
                )
            return False
        notification.status = NotificationStatus.SENT
        notification.sent_at = utcnow()
        notification.last_error = None
        notification.telegram_message_id = getattr(message, "message_id", None)
        return True
