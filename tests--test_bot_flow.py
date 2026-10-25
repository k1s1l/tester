"""End-to-end tests through the real aiogram Dispatcher with a mocked Telegram session.

Scenario 11 (non-admin uses admin command), 12 (double confirm via button), 14 (DB error handler)
and the full purchase flow: question -> stock check -> order -> name -> phone -> confirm -> admin notified.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import count

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage
from aiogram.types import CallbackQuery, Chat, Contact, ErrorEvent, Message, Update, User
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app import texts
from app.database.models import Order, OrderStatus, ProductSize
from app.handlers.common import on_error
from app.keyboards.admin import AdminOrderCb
from app.keyboards.client import CheckoutCb, DeliveryCb, OrderCb
from tests.conftest import ADMIN_CHAT, ADMIN_ID, CLIENT_ID
from tests.fakes import MockedSession, analysis

_ids = count(1)


@pytest.fixture(autouse=True)
def _reset(session_mock, module_ai):
    session_mock.clear()
    session_mock.fail_with = None
    module_ai.message_result = None
    module_ai.photo_result = None


def _user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Test", username=f"user{user_id}")


def message_update(user_id: int, text: str | None = None, contact: Contact | None = None) -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(
            message_id=next(_ids),
            date=datetime.now(),
            chat=Chat(id=user_id, type="private"),
            from_user=_user(user_id),
            text=text,
            contact=contact,
        ),
    )


def callback_update(user_id: int, data: str, chat_id: int | None = None) -> Update:
    chat_id = chat_id or user_id
    return Update(
        update_id=next(_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)),
            from_user=_user(user_id),
            chat_instance="ci",
            data=data,
            message=Message(message_id=next(_ids), date=datetime.now(), chat=Chat(id=chat_id, type="private"), text="x"),
        ),
    )


def callback_answers(session_mock: MockedSession) -> list[str]:
    return [r.text or "" for r in session_mock.requests if isinstance(r, AnswerCallbackQuery)]


# 11. A regular user calls an admin command
@pytest.mark.parametrize("command", ["/admin", "/orders", "/stats", "/add_product", "/export_orders", "/set_stock NV5 44 5"])
async def test_regular_user_cannot_use_admin_commands(dispatcher, bot, session_mock, command, session_factory):
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, command))
    assert session_mock.sent_texts(CLIENT_ID) == [texts.ADMIN_ONLY]
    async with session_factory() as session:  # /set_stock must not have changed anything
        row = await session.scalar(select(ProductSize).where(ProductSize.size == Decimal("44"), ProductSize.quantity > 0))
    assert row is None or row.product_id != 1


async def test_regular_user_cannot_press_admin_buttons(dispatcher, bot, session_mock):
    data = AdminOrderCb(action="confirmed", order_id=1).pack()
    await dispatcher.feed_update(bot, callback_update(CLIENT_ID, data, chat_id=ADMIN_CHAT))
    assert callback_answers(session_mock) == [texts.ADMIN_ONLY]


async def test_admin_can_open_panel(dispatcher, bot, session_mock):
    await dispatcher.feed_update(bot, message_update(ADMIN_ID, "/admin"))
    assert "Панель адміністратора" in session_mock.sent_texts(ADMIN_ID)[0]


async def test_full_purchase_flow(dispatcher, bot, session_mock, module_ai, session_factory):
    # 1. Free-form question
    module_ai.message_result = analysis("check_size", brand="Nike", model="Vomero 5", size="47", reply="Зараз гляну!")
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "Привіт, є Vomero 5 у 47?"))
    reply = session_mock.requests[-1]
    assert isinstance(reply, SendMessage) and "є в наявності" in reply.text
    order_cb = next(b.callback_data for row in reply.reply_markup.inline_keyboard for b in row if b.callback_data.startswith("o:"))
    assert OrderCb.unpack(order_cb).size == "47"

    # 2. "Оформити замовлення" -> ПІБ -> phone (shared contact) -> city -> delivery service -> branch
    session_mock.clear()
    await dispatcher.feed_update(bot, callback_update(CLIENT_ID, order_cb))
    assert texts.ORDER_ASK_NAME in session_mock.sent_texts(CLIENT_ID)
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "Микита"))  # only a first name: rejected
    assert session_mock.sent_texts(CLIENT_ID)[-1] == texts.ORDER_BAD_NAME
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "шевченко микита олегович"))
    assert "Дякую, Микита!" in session_mock.sent_texts(CLIENT_ID)[-1]
    contact = Contact(phone_number="380671234567", first_name="Микита", user_id=CLIENT_ID)
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, contact=contact))
    assert session_mock.sent_texts(CLIENT_ID)[-1] == texts.ORDER_ASK_CITY
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "Київ"))
    delivery_msg = session_mock.requests[-1]
    assert delivery_msg.text == texts.ORDER_ASK_DELIVERY
    np_cb = delivery_msg.reply_markup.inline_keyboard[0][0].callback_data
    assert DeliveryCb.unpack(np_cb).service == "nova_poshta"
    await dispatcher.feed_update(bot, callback_update(CLIENT_ID, np_cb))
    assert session_mock.sent_texts(CLIENT_ID)[-1] == texts.ORDER_ASK_POINT["nova_poshta"]
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "відділення"))  # no number
    assert session_mock.sent_texts(CLIENT_ID)[-1] == texts.ORDER_BAD_POINT["nova_poshta"]
    session_mock.clear()
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "12"))
    summary = session_mock.requests[-1]
    assert "Перевірте, будь ласка, замовлення" in summary.text
    assert "Розмір: <b>47</b>" in summary.text and "5800 грн" in summary.text and "+380671234567" in summary.text
    assert "Отримувач: Шевченко Микита Олегович" in summary.text
    assert "Доставка: Нова Пошта" in summary.text and "Місто: Київ" in summary.text
    assert "Відділення / адреса: Відділення №12" in summary.text
    confirm_cb = summary.reply_markup.inline_keyboard[0][0].callback_data
    assert CheckoutCb.unpack(confirm_cb).action == "confirm"

    # 3. Confirm -> order created, client + admin notified
    session_mock.clear()
    await dispatcher.feed_update(bot, callback_update(CLIENT_ID, confirm_cb))
    client_texts = session_mock.sent_texts(CLIENT_ID)
    assert any("Номер замовлення: <b>#1001</b>" in t for t in client_texts)
    admin_texts = session_mock.sent_texts(ADMIN_CHAT)
    assert admin_texts and "НОВЕ ЗАМОВЛЕННЯ #1001" in admin_texts[0] and "Статус: <b>Очікує підтвердження</b>" in admin_texts[0]
    for line in ("ПІБ: Шевченко Микита Олегович", "Телефон: +380671234567", "Доставка: Нова Пошта",
                 "Місто: Київ", "Відділення / адреса: Відділення №12"):
        assert line in admin_texts[0]
    assert not any("оплачено" in t.lower() for t in client_texts)

    # 4. Second press of the same confirm button does not create another order (scenario 12)
    session_mock.clear()
    await dispatcher.feed_update(bot, callback_update(CLIENT_ID, confirm_cb))
    assert callback_answers(session_mock) == [texts.ORDER_ALREADY_CREATED.format(number=1001)]
    async with session_factory() as session:
        orders = (await session.scalars(select(Order))).all()
    assert len(orders) == 1 and orders[0].customer_name == "Шевченко Микита Олегович"
    assert orders[0].contact == "+380671234567"
    assert (orders[0].delivery_service, orders[0].city, orders[0].delivery_point) == ("nova_poshta", "Київ", "Відділення №12")

    # 5. Admin confirms via button; repeated press is reported, not applied twice
    session_mock.clear()
    admin_cb = AdminOrderCb(action="confirmed", order_id=orders[0].id).pack()
    await dispatcher.feed_update(bot, callback_update(ADMIN_ID, admin_cb, chat_id=ADMIN_CHAT))
    await dispatcher.feed_update(bot, callback_update(ADMIN_ID, admin_cb, chat_id=ADMIN_CHAT))
    answers = callback_answers(session_mock)
    assert answers[0] == "Статус: Підтверджено" and "вже має статус" in answers[1]
    assert any("підтверджено" in t for t in session_mock.sent_texts(CLIENT_ID))

    # 6. Next order: saved recipient and delivery data are reused, nothing is asked again
    module_ai.message_result = analysis("start_order", brand="ASICS", model="GEL-1130", size="43")
    session_mock.clear()
    await dispatcher.feed_update(bot, message_update(CLIENT_ID, "Хочу замовити GEL-1130 43"))
    sent = session_mock.sent_texts(CLIENT_ID)
    assert any("Перевірте, будь ласка, замовлення" in t and "Відділення №12" in t for t in sent)
    assert texts.ORDER_ASK_NAME not in sent and texts.ORDER_ASK_CITY not in sent


async def test_checkout_cancel(dispatcher, bot, session_mock, module_ai, session_factory):
    module_ai.message_result = analysis("start_order", brand="Nike", model="Vomero 5", size="42")
    await dispatcher.feed_update(bot, message_update(7777, "беру vomero 42"))
    await dispatcher.feed_update(bot, message_update(7777, "❌ Скасувати оформлення"))
    assert session_mock.sent_texts(7777)[-1] == texts.ORDER_CANCELLED_DRAFT
    async with session_factory() as session:
        assert (await session.scalars(select(Order))).all() == []


async def test_invalid_phone_is_asked_again(dispatcher, bot, session_mock, module_ai):
    module_ai.message_result = analysis("start_order", brand="Nike", model="Vomero 5", size="42")
    await dispatcher.feed_update(bot, message_update(8888, "беру vomero 42"))
    await dispatcher.feed_update(bot, message_update(8888, "Коваль Олена Петрівна"))
    await dispatcher.feed_update(bot, message_update(8888, "не скажу"))
    assert session_mock.sent_texts(8888)[-1] == texts.ORDER_BAD_CONTACT


async def test_admin_order_status_rules_through_bot(dispatcher, bot, session_mock, session_factory):
    from tests.test_orders import _place

    order = (await _place(session_factory, dispatcher["settings"])).order
    await dispatcher.feed_update(bot, callback_update(ADMIN_ID, AdminOrderCb(action="shipped", order_id=order.id).pack(), ADMIN_CHAT))
    assert "Неможливо" in callback_answers(session_mock)[0]
    async with session_factory() as session:
        assert (await session.get(Order, order.id)).status == OrderStatus.PENDING


# 14. Database error while handling an update -> user gets a friendly Ukrainian message
async def test_db_error_handler(bot, session_mock):
    update = message_update(CLIENT_ID, "привіт")
    event = ErrorEvent(update=update, exception=OperationalError("SELECT 1", {}, Exception("connection refused")))
    update.message.as_(bot)
    assert await on_error(event) is True
    assert session_mock.sent_texts(CLIENT_ID) == [texts.ERROR_DB]


async def test_db_down_during_dialog(dispatcher, bot, session_mock, module_ai, monkeypatch):
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.database.session import create_session_factory

    broken_engine = create_async_engine("postgresql+asyncpg://postgres:wrong@127.0.0.1:1/nope")
    monkeypatch.setattr(dispatcher["assistant"], "_session_factory", create_session_factory(broken_engine))
    module_ai.message_result = analysis("search", brand="Nike", model="Vomero 5")
    await dispatcher.feed_update(bot, message_update(4242, "Nike Vomero 5"))
    assert session_mock.sent_texts(4242) == [texts.ERROR_DB]
    await broken_engine.dispose()
