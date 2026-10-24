"""Admin: order list, details and status buttons."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import texts
from app.config import Settings
from app.database.models import Order, OrderStatus, OrderStatusHistory
from app.database.session import SessionFactory
from app.keyboards.admin import (
    AdminOrderCb,
    order_actions_keyboard,
    orders_list_keyboard,
)
from app.services.notifications import NotificationSender
from app.services.order_rules import ADMIN_TARGETS
from app.services.orders import (
    AlreadyInStatus,
    InvalidTransition,
    OrderNotFound,
    change_status,
    get_order,
)

log = logging.getLogger(__name__)
router = Router(name="admin_orders")


async def orders_list_text(session_factory: SessionFactory, status: str | None) -> tuple[str, list[Order]]:
    async with session_factory() as session:
        stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id.desc()).limit(20)
        if status:
            stmt = stmt.where(Order.status == status)
        orders = list((await session.scalars(stmt)).all())
    if not orders:
        return "Замовлень не знайдено.", []
    lines = [f"<b>Замовлення{' · ' + texts.STATUS_LABELS[status] if status else ''}</b> (останні 20)"]
    for order in orders:
        item = order.items[0] if order.items else None
        desc = f"{texts.h(item.brand)} {texts.h(item.model)} р.{texts.fmt_size(item.size)}" if item else ""
        lines.append(
            f"#{order.number} · {order.created_at:%d.%m %H:%M} · {desc} · {texts.fmt_price(order.total_amount)} · "
            f"{texts.STATUS_LABELS.get(order.status, order.status)}"
        )
    return "\n".join(lines), orders


@router.message(Command("orders"))
async def cmd_orders(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    status = (command.args or "").strip().lower() or None
    if status and status not in set(OrderStatus):
        await message.answer("Невідомий статус. Доступні: " + ", ".join(s.value for s in OrderStatus))
        return
    text, orders = await orders_list_text(session_factory, status)
    await message.answer(text, reply_markup=orders_list_keyboard(orders) if orders else None)


async def order_details(session_factory: SessionFactory, order_id: int | None = None, number: int | None = None) -> tuple[str, Order] | None:
    async with session_factory() as session:
        order = await get_order(session, order_id=order_id, number=number)
        if order is None:
            return None
        history = (
            await session.scalars(
                select(OrderStatusHistory).where(OrderStatusHistory.order_id == order.id).order_by(OrderStatusHistory.id)
            )
        ).all()
    lines = [texts.admin_order_text(order), f"Створено: {order.created_at:%d.%m.%Y %H:%M} UTC", "", "<b>Історія:</b>"]
    for entry in history:
        actor = f" (ID {entry.actor_telegram_id})" if entry.actor_telegram_id else ""
        lines.append(f"{entry.created_at:%d.%m %H:%M} → {texts.STATUS_LABELS.get(entry.to_status, entry.to_status)}{actor}")
    return "\n".join(lines), order


@router.message(Command("order"))
async def cmd_order(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    arg = (command.args or "").strip().lstrip("#")
    if not arg.isdigit():
        await message.answer("Використання: /order 1001")
        return
    result = await order_details(session_factory, number=int(arg))
    if result is None:
        await message.answer("Замовлення не знайдено.")
        return
    text, order = result
    await message.answer(text, reply_markup=order_actions_keyboard(order.id, order.status))


@router.callback_query(AdminOrderCb.filter(F.action == "view"))
async def cb_view(callback: CallbackQuery, callback_data: AdminOrderCb, session_factory: SessionFactory) -> None:
    await callback.answer()
    result = await order_details(session_factory, order_id=callback_data.order_id)
    if result is None or not isinstance(callback.message, Message):
        return
    text, order = result
    await callback.message.answer(text, reply_markup=order_actions_keyboard(order.id, order.status))


@router.callback_query(AdminOrderCb.filter(F.action != "view"))
async def cb_change_status(
    callback: CallbackQuery,
    callback_data: AdminOrderCb,
    settings: Settings,
    session_factory: SessionFactory,
    sender: NotificationSender,
) -> None:
    # Defense in depth: the router filter already checked ADMIN_IDS.
    if not settings.is_admin(callback.from_user.id):
        await callback.answer(texts.ADMIN_ONLY, show_alert=True)
        return
    target = callback_data.action
    if target not in ADMIN_TARGETS:
        await callback.answer("Невідома дія.", show_alert=True)
        return
    try:
        async with session_factory() as session, session.begin():
            order = await change_status(
                session, settings, order_id=callback_data.order_id, target=target, actor_id=callback.from_user.id
            )
    except AlreadyInStatus as exc:
        await callback.answer(f"Замовлення вже має статус «{texts.STATUS_LABELS[exc.status]}».")
        await _refresh(callback, session_factory, callback_data.order_id)
        return
    except InvalidTransition as exc:
        await callback.answer(
            f"Неможливо: «{texts.STATUS_LABELS[exc.status]}» → «{texts.STATUS_LABELS[exc.target]}».",
            show_alert=True,
        )
        await _refresh(callback, session_factory, callback_data.order_id)
        return
    except OrderNotFound:
        await callback.answer("Замовлення не знайдено.", show_alert=True)
        return
    await callback.answer(f"Статус: {texts.STATUS_LABELS[order.status]}")
    await _edit(callback, order)
    await sender.deliver_due()


async def _refresh(callback: CallbackQuery, session_factory: SessionFactory, order_id: int) -> None:
    async with session_factory() as session:
        order = await get_order(session, order_id=order_id)
    if order is not None:
        await _edit(callback, order)


async def _edit(callback: CallbackQuery, order: Order) -> None:
    if not isinstance(callback.message, Message):
        return
    try:
        await callback.message.edit_text(texts.admin_order_text(order), reply_markup=order_actions_keyboard(order.id, order.status))
    except TelegramBadRequest:  # "message is not modified" or the message is too old
        pass
