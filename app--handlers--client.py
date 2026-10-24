"""Free-form client dialog: text, photos and inline buttons."""

from __future__ import annotations

import io
import logging

from aiogram import Bot, F, Router
from aiogram.enums import ChatAction
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app import texts
from app.config import Settings
from app.database.session import SessionFactory
from app.handlers.order import start_checkout
from app.handlers.utils import load_ctx, save_ctx, send_reply, user_info
from app.keyboards import client as kb
from app.schemas.catalog import parse_size
from app.services import catalog, waitlist
from app.services.assistant import BotReply, SalesAssistant
from app.services.images import ImageRejected, check_declared, validate_image
from app.services.notifications import NotificationSender
from app.services.orders import (
    AlreadyInStatus,
    InvalidTransition,
    NotOrderOwner,
    OrderNotFound,
    cancel_by_client,
)

log = logging.getLogger(__name__)
router = Router(name="client")
router.message.filter(F.chat.type == "private")


async def _deliver(
    message: Message,
    state: FSMContext,
    reply: BotReply,
    *,
    user_id: int,
    session_factory: SessionFactory,
) -> None:
    if reply.checkout is not None:
        await start_checkout(
            message,
            state,
            user_id=user_id,
            product_id=reply.checkout.product_id,
            size=reply.checkout.size,
            session_factory=session_factory,
        )
        return
    await send_reply(message, reply)


@router.message(F.photo | (F.document & F.document.mime_type.startswith("image/")))
async def on_photo(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    assistant: SalesAssistant,
    session_factory: SessionFactory,
) -> None:
    max_bytes = int(settings.max_photo_mb * 1024 * 1024)
    if message.photo:
        file = message.photo[-1]
        file_id, size, mime = file.file_id, file.file_size, None
    else:
        document = message.document
        assert document is not None
        file_id, size, mime = document.file_id, document.file_size, document.mime_type
    try:
        check_declared(size, mime, max_bytes)
        await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        buffer = io.BytesIO()
        await bot.download(file_id, destination=buffer)
        media_type = validate_image(buffer.getvalue(), max_bytes)
    except ImageRejected as exc:
        text = texts.PHOTO_TOO_BIG.format(mb=settings.max_photo_mb) if exc.reason == "too_big" else texts.PHOTO_BAD_TYPE
        await message.answer(text)
        return
    ctx = await load_ctx(state, settings)
    reply = await assistant.handle_photo(user_info(message.from_user), buffer.getvalue(), media_type, message.caption, ctx)
    await save_ctx(state, ctx)
    await _deliver(message, state, reply, user_id=message.from_user.id, session_factory=session_factory)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(
    message: Message,
    bot: Bot,
    state: FSMContext,
    settings: Settings,
    assistant: SalesAssistant,
    session_factory: SessionFactory,
) -> None:
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)
    ctx = await load_ctx(state, settings)
    reply = await assistant.handle_text(user_info(message.from_user), message.text or "", ctx)
    await save_ctx(state, ctx)
    await _deliver(message, state, reply, user_id=message.from_user.id, session_factory=session_factory)


@router.message(F.text.startswith("/"))
async def unknown_command(message: Message) -> None:
    await message.answer("Невідома команда. Просто напишіть, що шукаєте, або надішліть фото кросівок. /help — довідка")


@router.message()
async def unsupported(message: Message) -> None:
    await message.answer("Я розумію текстові повідомлення та фото кросівок. Напишіть, що шукаєте 🙂")


# ------------------------------------------------------------------ inline buttons


@router.callback_query(kb.ProductCb.filter(F.action == "show"))
async def cb_show_product(
    callback: CallbackQuery, callback_data: kb.ProductCb, state: FSMContext, settings: Settings, assistant: SalesAssistant
) -> None:
    await callback.answer()
    ctx = await load_ctx(state, settings)
    reply = await assistant.show_product(callback_data.product_id, ctx)
    await save_ctx(state, ctx)
    if isinstance(callback.message, Message):
        await send_reply(callback.message, reply)


@router.callback_query(kb.ProductCb.filter(F.action == "alts"))
async def cb_alternatives(
    callback: CallbackQuery, callback_data: kb.ProductCb, state: FSMContext, settings: Settings, assistant: SalesAssistant
) -> None:
    await callback.answer()
    ctx = await load_ctx(state, settings)
    reply = await assistant.alternatives(callback_data.product_id, callback_data.size, ctx)
    await save_ctx(state, ctx)
    if isinstance(callback.message, Message):
        await send_reply(callback.message, reply)


@router.callback_query(kb.SizeCb.filter())
async def cb_size(
    callback: CallbackQuery, callback_data: kb.SizeCb, state: FSMContext, settings: Settings, assistant: SalesAssistant
) -> None:
    await callback.answer()
    ctx = await load_ctx(state, settings)
    reply = await assistant.choose_size(callback_data.product_id, callback_data.size, ctx)
    await save_ctx(state, ctx)
    if isinstance(callback.message, Message):
        await send_reply(callback.message, reply)


@router.callback_query(kb.OrderCb.filter())
async def cb_order(
    callback: CallbackQuery,
    callback_data: kb.OrderCb,
    state: FSMContext,
    settings: Settings,
    assistant: SalesAssistant,
    session_factory: SessionFactory,
) -> None:
    await callback.answer()
    if not isinstance(callback.message, Message):
        return
    ctx = await load_ctx(state, settings)
    reply = await assistant.checkout_check(callback_data.product_id, callback_data.size, ctx)
    await save_ctx(state, ctx)
    await _deliver(callback.message, state, reply, user_id=callback.from_user.id, session_factory=session_factory)


@router.callback_query(kb.WaitlistCb.filter())
async def cb_waitlist(
    callback: CallbackQuery,
    callback_data: kb.WaitlistCb,
    state: FSMContext,
    settings: Settings,
    session_factory: SessionFactory,
    sender: NotificationSender,
) -> None:
    ctx = await load_ctx(state, settings)
    size = None
    if callback_data.size:
        try:
            size = parse_size(callback_data.size)
        except ValueError:
            size = None
    async with session_factory() as session, session.begin():
        product = await catalog.get_product(session, callback_data.product_id) if callback_data.product_id else None
        query = None
        if product is None and ctx.history:
            query = ctx.history[-2]["content"].replace("<customer_message>", "").replace("</customer_message>", "").strip()
        added = await waitlist.add_request(
            session, settings, telegram_id=callback.from_user.id, product=product, size=size, query=query
        )
    await callback.answer(texts.WAITLIST_SAVED if added else "Ви вже в списку очікування 👍", show_alert=True)
    if added:
        await sender.deliver_due()


@router.callback_query(kb.ClientOrderCb.filter(F.action == "cancel"))
async def cb_client_cancel(
    callback: CallbackQuery,
    callback_data: kb.ClientOrderCb,
    settings: Settings,
    session_factory: SessionFactory,
    sender: NotificationSender,
) -> None:
    try:
        async with session_factory() as session, session.begin():
            order = await cancel_by_client(
                session, settings, order_id=callback_data.order_id, telegram_id=callback.from_user.id
            )
    except (OrderNotFound, NotOrderOwner):
        await callback.answer("Замовлення не знайдено.", show_alert=True)
        return
    except AlreadyInStatus:
        await callback.answer("Це замовлення вже скасовано.", show_alert=True)
        return
    except InvalidTransition as exc:
        status = texts.STATUS_LABELS.get(exc.status, exc.status)
        await callback.answer(
            f"Замовлення має статус «{status}», його вже не можна скасувати тут. Напишіть, будь ласка, адміністратору.",
            show_alert=True,
        )
        return
    await callback.answer("Замовлення скасовано")
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(texts.CLIENT_STATUS_MESSAGES["cancelled"].format(number=order.number))
    await sender.deliver_due()
