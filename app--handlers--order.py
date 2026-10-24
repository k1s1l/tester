"""Checkout: product + size -> name -> contact -> summary -> confirm."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove
from sqlalchemy import select

from app import texts
from app.config import Settings
from app.database.models import Customer, Order
from app.database.session import SessionFactory
from app.handlers.utils import load_ctx, save_ctx, send_reply
from app.keyboards import client as kb
from app.schemas.order import normalize_contact, normalize_name, normalize_phone
from app.services import catalog
from app.services.assistant import SalesAssistant
from app.services.notifications import NotificationSender
from app.services.orders import (
    InsufficientStock,
    ProductUnavailable,
    TooManyOpenOrders,
    place_order,
)

log = logging.getLogger(__name__)
router = Router(name="order")
router.message.filter(F.chat.type == "private")


class OrderFlow(StatesGroup):
    name = State()
    contact = State()
    confirm = State()


async def start_checkout(
    message: Message,
    state: FSMContext,
    *,
    user_id: int,
    product_id: int,
    size: Decimal,
    session_factory: SessionFactory,
) -> None:
    """Entry point used by the client dialog once stock was re-checked."""
    async with session_factory() as session:
        product = await catalog.get_product(session, product_id)
        customer = await session.scalar(select(Customer).where(Customer.telegram_id == user_id))
    if product is None or not product.is_active:
        await message.answer(texts.ORDER_DRAFT_MISSING)
        return

    data = await state.get_data()
    profile = dict(data.get("profile") or {})
    if customer is not None:
        profile["name"] = profile.get("name") or customer.full_name
        profile["contact"] = profile.get("contact") or customer.contact

    draft = {
        "token": uuid.uuid4().hex,
        "product_id": product.id,
        "size": texts.fmt_size(size),
        "title": product.title,
        "price": str(product.price),
        "is_demo": product.is_demo,
    }
    await state.update_data(draft=draft, profile=profile)

    if profile.get("name") and profile.get("contact"):
        await _show_summary(message, state)
        return
    if profile.get("name"):
        await state.set_state(OrderFlow.contact)
        await message.answer(
            texts.ORDER_ASK_CONTACT.format(name=texts.h(profile["name"])), reply_markup=kb.contact_request_keyboard()
        )
        return
    await state.set_state(OrderFlow.name)
    await message.answer(texts.ORDER_ASK_NAME, reply_markup=kb.cancel_only_keyboard())


async def _show_summary(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft, profile = data["draft"], data["profile"]
    await state.set_state(OrderFlow.confirm)
    await message.answer("Дякую! 🙌", reply_markup=ReplyKeyboardRemove())
    await message.answer(
        texts.order_summary(
            title=draft["title"],
            size=Decimal(draft["size"]),
            price=Decimal(draft["price"]),
            name=profile["name"],
            contact=profile["contact"],
            is_demo=draft["is_demo"],
        ),
        reply_markup=kb.checkout_confirm_keyboard(draft["token"]),
    )


async def _cancel_draft(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(draft=None)
    await message.answer(texts.ORDER_CANCELLED_DRAFT, reply_markup=ReplyKeyboardRemove())


@router.message(StateFilter(OrderFlow), F.text == kb.CANCEL_TEXT)
@router.message(StateFilter(OrderFlow), Command("cancel"))
async def cancel_text(message: Message, state: FSMContext) -> None:
    await _cancel_draft(message, state)


@router.message(OrderFlow.name, F.text)
async def got_name(message: Message, state: FSMContext) -> None:
    name = normalize_name(message.text or "")
    if name is None:
        await message.answer(texts.ORDER_BAD_NAME, reply_markup=kb.cancel_only_keyboard())
        return
    data = await state.get_data()
    profile = dict(data.get("profile") or {})
    profile["name"] = name
    await state.update_data(profile=profile)
    if profile.get("contact"):
        await _show_summary(message, state)
        return
    await state.set_state(OrderFlow.contact)
    await message.answer(texts.ORDER_ASK_CONTACT.format(name=texts.h(name)), reply_markup=kb.contact_request_keyboard())


@router.message(OrderFlow.contact, F.contact)
async def got_shared_contact(message: Message, state: FSMContext) -> None:
    contact = message.contact
    if contact is None or message.from_user is None or contact.user_id != message.from_user.id:
        await message.answer(texts.ORDER_FOREIGN_CONTACT, reply_markup=kb.contact_request_keyboard())
        return
    phone = normalize_phone(contact.phone_number)
    if phone is None:
        await message.answer(texts.ORDER_BAD_CONTACT, reply_markup=kb.contact_request_keyboard())
        return
    await _save_contact(message, state, phone)


@router.message(OrderFlow.contact, F.text)
async def got_text_contact(message: Message, state: FSMContext) -> None:
    contact = normalize_contact(message.text or "")
    if contact is None:
        await message.answer(texts.ORDER_BAD_CONTACT, reply_markup=kb.contact_request_keyboard())
        return
    await _save_contact(message, state, contact)


async def _save_contact(message: Message, state: FSMContext, contact: str) -> None:
    data = await state.get_data()
    profile = dict(data.get("profile") or {})
    profile["contact"] = contact
    await state.update_data(profile=profile)
    await _show_summary(message, state)


@router.message(OrderFlow.confirm)
async def confirm_reminder(message: Message) -> None:
    await message.answer("Будь ласка, підтвердіть або скасуйте замовлення кнопками вище. 👆")


@router.message(StateFilter(OrderFlow.name, OrderFlow.contact))
async def unexpected_content(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current == OrderFlow.name.state:
        await message.answer(texts.ORDER_ASK_NAME, reply_markup=kb.cancel_only_keyboard())
    else:
        await message.answer(texts.ORDER_BAD_CONTACT, reply_markup=kb.contact_request_keyboard())


@router.callback_query(kb.CheckoutCb.filter(F.action == "confirm"))
async def confirm_order(
    callback: CallbackQuery,
    callback_data: kb.CheckoutCb,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
    assistant: SalesAssistant,
    sender: NotificationSender,
) -> None:
    message = callback.message
    if not isinstance(message, Message):
        await callback.answer()
        return
    data = await state.get_data()
    draft, profile = data.get("draft"), data.get("profile") or {}

    if not draft or draft.get("token") != callback_data.token:
        # Repeated press / stale button: never create a second order.
        async with session_factory() as session:
            existing = await session.scalar(select(Order).where(Order.idempotency_key == callback_data.token))
        await callback.answer(
            texts.ORDER_ALREADY_CREATED.format(number=existing.number) if existing else texts.ORDER_DRAFT_MISSING,
            show_alert=True,
        )
        return
    if not profile.get("name") or not profile.get("contact"):
        await callback.answer(texts.ORDER_DRAFT_MISSING, show_alert=True)
        return
    await callback.answer("Оформлюю замовлення…")
    user = callback.from_user
    size = Decimal(draft["size"])
    try:
        result = await place_order(
            session_factory,
            settings,
            telegram_id=user.id,
            username=user.username,
            telegram_name=user.full_name,
            product_id=int(draft["product_id"]),
            size=size,
            customer_name=profile["name"],
            contact=profile["contact"],
            idempotency_key=callback_data.token,
        )
    except (InsufficientStock, ProductUnavailable):
        await state.set_state(None)
        await state.update_data(draft=None)
        await _drop_buttons(message)
        await message.answer(texts.ORDER_SIZE_GONE.format(size=texts.fmt_size(size), title=texts.h(draft["title"])))
        ctx = await load_ctx(state, settings)
        reply = await assistant.alternatives(int(draft["product_id"]), draft["size"], ctx)
        await save_ctx(state, ctx)
        await send_reply(message, reply)
        return
    except TooManyOpenOrders:
        await message.answer(texts.ORDER_TOO_MANY)
        return

    await state.set_state(None)
    await state.update_data(draft=None)
    await _drop_buttons(message)
    order = result.order
    await message.answer(texts.order_created(order), reply_markup=kb.client_order_keyboard(order.id))
    if result.created:
        try:
            await sender.deliver_due()
        except Exception:  # the worker will retry; the order is already saved
            log.exception("Immediate notification delivery failed for order #%s", order.number)


@router.callback_query(kb.CheckoutCb.filter(F.action == "cancel"))
async def cancel_order_draft(callback: CallbackQuery, callback_data: kb.CheckoutCb, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    await callback.answer()
    if isinstance(callback.message, Message):
        await _drop_buttons(callback.message)
        if draft and draft.get("token") == callback_data.token:
            await _cancel_draft(callback.message, state)


@router.callback_query(kb.CheckoutCb.filter(F.action == "edit"))
async def edit_profile(callback: CallbackQuery, callback_data: kb.CheckoutCb, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data.get("draft")
    await callback.answer()
    if not draft or draft.get("token") != callback_data.token or not isinstance(callback.message, Message):
        return
    await _drop_buttons(callback.message)
    await state.update_data(profile={})
    await state.set_state(OrderFlow.name)
    await callback.message.answer(texts.ORDER_ASK_NAME, reply_markup=kb.cancel_only_keyboard())


async def _drop_buttons(message: Message) -> None:
    try:
        await message.edit_reply_markup(reply_markup=None)
    except TelegramBadRequest:
        pass
