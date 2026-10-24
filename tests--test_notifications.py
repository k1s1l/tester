"""Scenario 13: admin notification cannot be delivered; restock notifications."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import select, update

from app.database.models import Notification, NotificationStatus, Order, Product
from app.services import catalog, inventory, waitlist
from app.services.notifications import NotificationSender
from app.services.orders import place_order
from tests.conftest import ADMIN_CHAT, CLIENT_ID


class FlakyBot:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.sent: list[dict] = []

    async def send_message(self, **kwargs):
        if self.error is not None:
            raise self.error
        self.sent.append(kwargs)
        return type("Msg", (), {"message_id": 42})()


async def _order(session_factory, settings):
    async with session_factory() as session:
        product_id = await session.scalar(select(Product.id).where(Product.sku == "NV5"))
    return await place_order(
        session_factory, settings, telegram_id=CLIENT_ID, username=None, telegram_name="T", product_id=product_id,
        size=Decimal("42"), customer_name="Микита", contact="+380671234567", idempotency_key=uuid.uuid4().hex,
    )


async def test_telegram_outage_keeps_order_and_retries(session_factory, settings):
    result = await _order(session_factory, settings)
    error = TelegramNetworkError(method=SendMessage(chat_id=ADMIN_CHAT, text="x"), message="Network is unreachable")
    bot = FlakyBot(error)
    sender = NotificationSender(bot, session_factory, settings)

    assert await sender.deliver_due() == 0
    async with session_factory() as session:
        note = await session.scalar(select(Notification))
        order = await session.get(Order, result.order.id)
    assert order is not None  # the order is safely stored
    assert note.status == NotificationStatus.PENDING and note.attempts == 1
    assert "Network is unreachable" in note.last_error
    assert note.next_attempt_at > inventory.utcnow()

    # Not due yet -> nothing is sent even if Telegram is back
    bot.error = None
    assert await sender.deliver_due() == 0
    async with session_factory() as session, session.begin():
        await session.execute(update(Notification).values(next_attempt_at=inventory.utcnow() - timedelta(seconds=1)))
    assert await sender.deliver_due() == 1
    assert bot.sent[0]["chat_id"] == ADMIN_CHAT
    markup = bot.sent[0]["reply_markup"]
    labels = [b.text for row in markup.inline_keyboard for b in row]
    assert "✅ Підтвердити" in labels and "❌ Відхилити" in labels
    async with session_factory() as session:
        note = await session.scalar(select(Notification))
    assert note.status == NotificationStatus.SENT and note.telegram_message_id == 42


async def test_gives_up_after_max_attempts(session_factory, settings):
    await _order(session_factory, settings)
    bot = FlakyBot(TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="down"))
    sender = NotificationSender(bot, session_factory, settings)
    for _ in range(settings.notification_max_attempts):
        async with session_factory() as session, session.begin():
            await session.execute(update(Notification).values(next_attempt_at=inventory.utcnow() - timedelta(seconds=1)))
        await sender.deliver_due()
    async with session_factory() as session:
        note = await session.scalar(select(Notification))
    assert note.status == NotificationStatus.FAILED and note.attempts == settings.notification_max_attempts


async def test_permanent_error_marks_failed(session_factory, settings):
    await _order(session_factory, settings)
    bot = FlakyBot(TelegramForbiddenError(method=SendMessage(chat_id=1, text="x"), message="bot was blocked by the user"))
    await NotificationSender(bot, session_factory, settings).deliver_due()
    async with session_factory() as session:
        note = await session.scalar(select(Notification))
    assert note.status == NotificationStatus.FAILED and "blocked" in note.last_error


async def test_restock_notifies_waitlist(session_factory, settings):
    async with session_factory() as session, session.begin():
        product = await catalog.get_product_by_sku(session, "NV5")
        assert await waitlist.add_request(session, settings, telegram_id=CLIENT_ID, product=product, size=Decimal("44"), query=None)
        assert not await waitlist.add_request(session, settings, telegram_id=CLIENT_ID, product=product, size=Decimal("44"), query=None)
    async with session_factory() as session, session.begin():
        product = await catalog.get_product_by_sku(session, "NV5")
        row, previous = await inventory.set_quantity(session, product_id=product.id, size=Decimal("44"), quantity=2)
        assert previous == 0 and row.available == 2
        assert await waitlist.notify_restock(session, product, Decimal("44")) == 1
    async with session_factory() as session:
        note = await session.scalar(select(Notification).where(Notification.kind == "client_restock"))
    assert note.chat_id == CLIENT_ID and "знову в наявності" in note.text
