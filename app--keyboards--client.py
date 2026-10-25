from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from aiogram.filters.callback_data import CallbackData
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database.models import Product
from app.texts import fmt_size


class ProductCb(CallbackData, prefix="p"):
    action: str  # show | alts
    product_id: int
    size: str = ""


class SizeCb(CallbackData, prefix="s"):
    product_id: int
    size: str


class OrderCb(CallbackData, prefix="o"):
    product_id: int
    size: str


class WaitlistCb(CallbackData, prefix="w"):
    product_id: int  # 0 = no concrete product
    size: str = ""


class CheckoutCb(CallbackData, prefix="c"):
    action: str  # confirm | cancel | edit
    token: str


class DeliveryCb(CallbackData, prefix="dl"):
    service: str  # nova_poshta | ukrposhta | meest


class ClientOrderCb(CallbackData, prefix="co"):
    action: str  # cancel
    order_id: int


CANCEL_TEXT = "❌ Скасувати оформлення"
SHARE_CONTACT_TEXT = "📱 Поділитися номером"


def products_keyboard(products: Sequence[Product]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for index, product in enumerate(products, start=1):
        builder.button(
            text=f"{index}. {product.title}",
            callback_data=ProductCb(action="show", product_id=product.id),
        )
    builder.adjust(1)
    return builder.as_markup()


def sizes_keyboard(product_id: int, sizes: Sequence[Decimal]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for size in sizes:
        label = fmt_size(size)
        builder.button(text=label, callback_data=SizeCb(product_id=product_id, size=label))
    builder.adjust(5)
    return builder.as_markup()


def order_offer_keyboard(product_id: int, size: Decimal) -> InlineKeyboardMarkup:
    label = fmt_size(size)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Оформити замовлення", callback_data=OrderCb(product_id=product_id, size=label).pack())],
            [InlineKeyboardButton(text="🔄 Схожі моделі", callback_data=ProductCb(action="alts", product_id=product_id, size=label).pack())],
        ]
    )


def alternatives_keyboard(products: Sequence[Product], size: Decimal | None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    label = fmt_size(size) if size is not None else ""
    for index, product in enumerate(products, start=1):
        if size is not None:
            builder.button(
                text=f"🛒 {index}. {product.title} — {label}",
                callback_data=OrderCb(product_id=product.id, size=label),
            )
        else:
            builder.button(text=f"{index}. {product.title}", callback_data=ProductCb(action="show", product_id=product.id))
    builder.adjust(1)
    return builder.as_markup()


def waitlist_keyboard(product_id: int | None, size: Decimal | None) -> InlineKeyboardMarkup:
    cb = WaitlistCb(product_id=product_id or 0, size=fmt_size(size) if size is not None else "")
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔔 Повідомити про надходження", callback_data=cb.pack())]]
    )


def checkout_confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Підтвердити", callback_data=CheckoutCb(action="confirm", token=token).pack()),
                InlineKeyboardButton(text="❌ Скасувати", callback_data=CheckoutCb(action="cancel", token=token).pack()),
            ],
            [InlineKeyboardButton(text="✏️ Змінити дані отримувача", callback_data=CheckoutCb(action="edit", token=token).pack())],
        ]
    )


def contact_request_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=SHARE_CONTACT_TEXT, request_contact=True)],
            [KeyboardButton(text=CANCEL_TEXT)],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def cancel_only_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=CANCEL_TEXT)]], resize_keyboard=True)


def delivery_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔴 Нова Пошта", callback_data=DeliveryCb(service="nova_poshta").pack())],
            [InlineKeyboardButton(text="🟡 Укрпошта", callback_data=DeliveryCb(service="ukrposhta").pack())],
            [InlineKeyboardButton(text="🔵 Meest Пошта", callback_data=DeliveryCb(service="meest").pack())],
        ]
    )


def client_order_keyboard(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Скасувати замовлення", callback_data=ClientOrderCb(action="cancel", order_id=order_id).pack())]
        ]
    )
