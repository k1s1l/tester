"""/start, /help, /my_orders, /forget, admin-command guard and the global error handler
(``on_error`` is registered on the dispatcher in app.main)."""

from __future__ import annotations

import logging

import asyncpg
from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app import texts
from app.database.models import Customer, Order
from app.database.session import SessionFactory
from app.keyboards.client import ClientOrderCb
from app.services.order_rules import CLIENT_CANCELLABLE

log = logging.getLogger(__name__)
router = Router(name="common")

ADMIN_COMMANDS = (
    "admin", "add_product", "edit_product", "products", "stock", "set_stock",
    "orders", "order", "stats", "export_orders",
)
ADMIN_CALLBACK_PREFIXES = ("ao:", "am:", "ap:", "apg:", "acat:", "dr:")


@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await state.update_data(draft=None, dialog=None)
    await message.answer(texts.GREETING, reply_markup=ReplyKeyboardRemove())


@router.message(Command("help"), F.chat.type == "private")
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP_CLIENT)


@router.message(Command("my_orders"), F.chat.type == "private")
async def cmd_my_orders(message: Message, session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        orders = (
            await session.scalars(
                select(Order)
                .options(selectinload(Order.items))
                .where(Order.telegram_id == message.from_user.id)
                .order_by(Order.id.desc())
                .limit(10)
            )
        ).all()
    builder = InlineKeyboardBuilder()
    for order in orders:
        if order.status in CLIENT_CANCELLABLE:
            builder.button(
                text=f"Скасувати #{order.number}", callback_data=ClientOrderCb(action="cancel", order_id=order.id)
            )
    builder.adjust(2)
    markup: InlineKeyboardMarkup | None = builder.as_markup() if builder.buttons else None
    await message.answer(texts.my_orders(orders), reply_markup=markup)


@router.message(Command("forget"), F.chat.type == "private")
async def cmd_forget(message: Message, state: FSMContext, session_factory: SessionFactory) -> None:
    await state.clear()
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Customer)
            .where(Customer.telegram_id == message.from_user.id)
            .values(full_name=None, contact=None, username=None, telegram_name=None)
        )
    await message.answer(
        "Історію діалогу та збережені ім'я й контакт видалено. Дані вже оформлених замовлень зберігаються "
        "лише для їх виконання та обліку.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(Command(*ADMIN_COMMANDS))
async def admin_command_denied(message: Message) -> None:
    # Reached only when the admin router's IsAdmin filter rejected the user.
    log.warning("User %s tried admin command %s", message.from_user.id if message.from_user else None, (message.text or "")[:30])
    await message.answer(texts.ADMIN_ONLY)


@router.callback_query(F.data.startswith(ADMIN_CALLBACK_PREFIXES))
async def admin_callback_denied(callback: CallbackQuery) -> None:
    log.warning("User %s pressed an admin button", callback.from_user.id)
    await callback.answer(texts.ADMIN_ONLY, show_alert=True)


async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    # asyncpg raises plain ConnectionError/OSError subclasses when the server is unreachable
    is_db = isinstance(exc, (SQLAlchemyError, asyncpg.PostgresError, asyncpg.InterfaceError, ConnectionError))
    log.error(
        "Unhandled %s while processing update %s",
        type(exc).__name__,
        event.update.update_id,
        exc_info=exc,
    )
    text = texts.ERROR_DB if is_db else texts.ERROR_GENERIC
    update_obj = event.update
    try:
        if update_obj.message is not None:
            await update_obj.message.answer(text)
        elif update_obj.callback_query is not None:
            await update_obj.callback_query.answer(text, show_alert=True)
    except Exception:  # Telegram itself may be unavailable
        log.warning("Could not deliver error message to the user")
    return True
