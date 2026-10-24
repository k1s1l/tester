from __future__ import annotations

from collections.abc import Sequence

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database.models import Order, OrderStatus, Product
from app.services.order_rules import admin_actions

ACTION_LABELS = {
    OrderStatus.CONFIRMED: "✅ Підтвердити",
    OrderStatus.REJECTED: "❌ Відхилити",
    OrderStatus.PAID: "💳 Оплачено",
    OrderStatus.SHIPPED: "🚚 Відправлено",
    OrderStatus.COMPLETED: "🏁 Завершити",
    OrderStatus.CANCELLED: "🚫 Скасувати",
}


class AdminOrderCb(CallbackData, prefix="ao"):
    action: str  # target status or "view"
    order_id: int


class AdminMenuCb(CallbackData, prefix="am"):
    action: str


class AdminProductCb(CallbackData, prefix="ap"):
    action: str  # view | price | description | color | name | sizes | photo | clear_photos | toggle
    product_id: int


class AdminPageCb(CallbackData, prefix="apg"):
    kind: str  # products
    page: int
    query: str = ""


class AdminCategoryCb(CallbackData, prefix="acat"):
    category: str


def order_actions_keyboard(order_id: int, status: str) -> InlineKeyboardMarkup | None:
    builder = InlineKeyboardBuilder()
    for target in admin_actions(status):
        builder.button(text=ACTION_LABELS[target], callback_data=AdminOrderCb(action=target, order_id=order_id))
    builder.adjust(2)
    markup = builder.as_markup()
    return markup if markup.inline_keyboard else None


def orders_list_keyboard(orders: Sequence[Order]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for order in orders:
        builder.button(text=f"#{order.number}", callback_data=AdminOrderCb(action="view", order_id=order.id))
    builder.adjust(5)
    return builder.as_markup()


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [("📦 Каталог", "products"), ("📊 Залишки", "stock")],
        [("🧾 Нові замовлення", "orders_pending"), ("📋 Усі замовлення", "orders")],
        [("📈 Статистика", "stats"), ("⬇️ Експорт CSV", "export")],
        [("➕ Додати товар", "add_product"), ("❓ Допомога", "help")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=AdminMenuCb(action=action).pack()) for text, action in row]
            for row in rows
        ]
    )


def product_edit_keyboard(product: Product) -> InlineKeyboardMarkup:
    pid = product.id
    toggle = "🙈 Приховати" if product.is_active else "👁 Активувати"
    rows = [
        [("💰 Ціна", "price"), ("🎨 Колір", "color")],
        [("🏷 Назва", "name"), ("📝 Опис", "description")],
        [("📏 Розміри/залишки", "sizes"), ("🖼 Додати фото", "photo")],
        [("🗑 Видалити фото", "clear_photos"), (toggle, "toggle")],
    ]
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=text, callback_data=AdminProductCb(action=action, product_id=pid).pack())
                for text, action in row
            ]
            for row in rows
        ]
    )


def products_page_keyboard(products: Sequence[Product], page: int, has_next: bool, query: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for product in products:
        mark = "" if product.is_active else "🙈 "
        builder.button(
            text=f"{mark}{product.sku} · {product.title}",
            callback_data=AdminProductCb(action="view", product_id=product.id),
        )
    builder.adjust(1)
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=AdminPageCb(kind="products", page=page - 1, query=query).pack()))
    if has_next:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=AdminPageCb(kind="products", page=page + 1, query=query).pack()))
    if nav:
        builder.row(*nav)
    return builder.as_markup()


def categories_keyboard(categories: Sequence[str]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for category in categories:
        builder.button(text=category, callback_data=AdminCategoryCb(category=category))
    builder.adjust(3)
    return builder.as_markup()
