"""Catalog import from a Telegram channel: new channel posts, posts forwarded by an admin, draft buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, MessageOriginChannel
from pydantic import ValidationError

from app import texts
from app.config import Settings
from app.database.models import DraftStatus
from app.database.session import SessionFactory
from app.handlers.admin.products import _parse_price
from app.keyboards.admin import DraftCb
from app.schemas.catalog import parse_sizes_spec
from app.services.channel_import import (
    ChannelImporter,
    DraftAlreadyHandled,
    DraftInvalid,
    IncomingPost,
    save_draft,
    skip_draft,
    update_draft,
)
from app.services.notifications import NotificationSender

log = logging.getLogger(__name__)
router = Router(name="admin_channel")

NOT_COMMAND = F.text & ~F.text.startswith("/")


class DraftEdit(StatesGroup):
    price = State()
    sizes = State()


def _photo_ids(message: Message) -> list[str]:
    return [message.photo[-1].file_id] if message.photo else []


# ------------------------------------------------------------------ intake


@router.channel_post(F.photo | F.text)
async def on_channel_post(message: Message, settings: Settings, importer: ChannelImporter) -> None:
    """New post in the shop channel (the bot must be an administrator of the channel)."""
    if settings.catalog_channel_id is not None and message.chat.id != settings.catalog_channel_id:
        return
    post = IncomingPost(
        source_chat_id=message.chat.id,
        source_message_id=message.message_id,
        text=message.caption or message.text or "",
        photo_file_ids=_photo_ids(message),
        reply_chat_ids=settings.admin_notification_chats,
        from_forward=False,
    )
    album_key = f"ch:{message.chat.id}:{message.media_group_id}" if message.media_group_id else None
    await importer.add_part(post, album_key=album_key)


@router.message(F.forward_origin.type == "channel")
async def on_forwarded_post(message: Message, importer: ChannelImporter) -> None:
    """Old posts forwarded by an admin to the bot."""
    origin = message.forward_origin
    if not isinstance(origin, MessageOriginChannel):
        return
    post = IncomingPost(
        source_chat_id=origin.chat.id,
        source_message_id=origin.message_id,
        text=message.caption or message.text or "",
        photo_file_ids=_photo_ids(message),
        reply_chat_ids=[message.chat.id],
        from_forward=True,
    )
    album_key = f"fwd:{message.chat.id}:{message.media_group_id}" if message.media_group_id else None
    await importer.add_part(post, album_key=album_key)


# ------------------------------------------------------------------ draft buttons


async def _drop_buttons(callback: CallbackQuery) -> None:
    if isinstance(callback.message, Message):
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass


@router.callback_query(DraftCb.filter(F.action.in_({"new", "update"})))
async def cb_save(
    callback: CallbackQuery,
    callback_data: DraftCb,
    settings: Settings,
    session_factory: SessionFactory,
    sender: NotificationSender,
) -> None:
    try:
        result = await save_draft(
            session_factory, settings, draft_id=callback_data.draft_id, mode=callback_data.action, actor=callback.from_user.id
        )
    except DraftAlreadyHandled as exc:
        await callback.answer("Цю чернетку вже збережено." if exc.status == DraftStatus.SAVED else "Цю чернетку пропущено.")
        await _drop_buttons(callback)
        return
    except (DraftInvalid, ValidationError, ValueError) as exc:
        await callback.answer(f"❗ {exc}"[:190], show_alert=True)
        return
    await callback.answer("Збережено ✅")
    await _drop_buttons(callback)
    product = result.product
    action = "Додано новий товар" if result.created else "Оновлено товар"
    extra = f"\nСповіщено клієнтів зі списку очікування: {result.restock_notified}" if result.restock_notified else ""
    if isinstance(callback.message, Message):
        await callback.message.answer(
            f"✅ {action} <code>{texts.h(product.sku)}</code>{extra}\n\n{texts.product_card(product)}\n\n"
            f"Змінити: /edit_product {texts.h(product.sku)}"
        )
    if result.restock_notified:
        await sender.deliver_due()


@router.callback_query(DraftCb.filter(F.action == "skip"))
async def cb_skip(callback: CallbackQuery, callback_data: DraftCb, session_factory: SessionFactory) -> None:
    try:
        await skip_draft(session_factory, callback_data.draft_id)
    except (DraftAlreadyHandled, DraftInvalid):
        await callback.answer("Чернетку вже оброблено.")
        await _drop_buttons(callback)
        return
    await callback.answer("Пропущено")
    await _drop_buttons(callback)


@router.callback_query(DraftCb.filter(F.action.in_({"price", "sizes"})))
async def cb_edit(callback: CallbackQuery, callback_data: DraftCb, state: FSMContext) -> None:
    await callback.answer()
    await state.update_data(draft_edit=callback_data.draft_id)
    if callback_data.action == "price":
        await state.set_state(DraftEdit.price)
        prompt = "Вкажіть ціну в гривнях, наприклад: 4500\n/cancel — скасувати"
    else:
        await state.set_state(DraftEdit.sizes)
        prompt = "Вкажіть розміри та кількість пар у форматі: 40:1, 41:2, 42:1\n/cancel — скасувати"
    if isinstance(callback.message, Message):
        await callback.message.answer(prompt)


async def _after_edit(message: Message, state: FSMContext, importer: ChannelImporter, draft_id: int) -> None:
    await state.set_state(None)
    await message.answer("✅ Чернетку оновлено:")
    await importer.send_draft_card(draft_id, [message.chat.id])


@router.message(DraftEdit.price, NOT_COMMAND)
async def edit_price(message: Message, state: FSMContext, session_factory: SessionFactory, importer: ChannelImporter) -> None:
    price = _parse_price(message.text or "")
    if price is None:
        await message.answer("Вкажіть ціну числом більше нуля, наприклад 4500.")
        return
    draft_id = int((await state.get_data()).get("draft_edit") or 0)
    try:
        await update_draft(session_factory, draft_id, price=str(price))
    except (DraftInvalid, DraftAlreadyHandled):
        await state.set_state(None)
        await message.answer("Ця чернетка вже неактуальна.")
        return
    await _after_edit(message, state, importer, draft_id)


@router.message(DraftEdit.sizes, NOT_COMMAND)
async def edit_sizes(message: Message, state: FSMContext, session_factory: SessionFactory, importer: ChannelImporter) -> None:
    try:
        rows = parse_sizes_spec(message.text or "")
    except (ValueError, ValidationError) as exc:
        await message.answer(f"❗ {texts.h(exc)}\nФормат: 40:1, 41:2, 42:1")
        return
    sizes = [
        {"size": str(row.size), "quantity": row.quantity, "guessed": False}
        for row in sorted(rows, key=lambda r: r.size)
    ]
    draft_id = int((await state.get_data()).get("draft_edit") or 0)
    try:
        await update_draft(session_factory, draft_id, sizes=sizes)
    except (DraftInvalid, DraftAlreadyHandled):
        await state.set_state(None)
        await message.answer("Ця чернетка вже неактуальна.")
        return
    await _after_edit(message, state, importer, draft_id)

