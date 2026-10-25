"""Admin statistics and CSV export."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import texts
from app.database.models import Order, OrderItem, Product, ProductSize
from app.services.inventory import utcnow
from app.services.order_rules import REVENUE_STATUSES


async def stats_text(session: AsyncSession) -> str:
    now = utcnow()
    by_status = dict(
        (await session.execute(select(Order.status, func.count(Order.id)).group_by(Order.status))).all()
    )
    lines = ["<b>📈 Статистика</b>", "", "<b>Замовлення за статусами:</b>"]
    for status, label in texts.STATUS_LABELS.items():
        lines.append(f"{label}: {by_status.get(status, 0)}")

    for days in (7, 30):
        count, revenue = (
            await session.execute(
                select(func.count(Order.id), func.coalesce(func.sum(Order.total_amount), 0)).where(
                    Order.status.in_(REVENUE_STATUSES), Order.created_at >= now - timedelta(days=days)
                )
            )
        ).one()
        lines.append(f"\nПродажі за {days} днів: {count} шт. на {texts.fmt_price(Decimal(revenue))}")

    top = (
        await session.execute(
            select(OrderItem.brand, OrderItem.model, func.sum(OrderItem.quantity).label("qty"))
            .join(Order, Order.id == OrderItem.order_id)
            .where(Order.status.in_(REVENUE_STATUSES))
            .group_by(OrderItem.brand, OrderItem.model)
            .order_by(func.sum(OrderItem.quantity).desc())
            .limit(5)
        )
    ).all()
    if top:
        lines.append("\n<b>Топ моделей:</b>")
        lines += [f"{texts.h(b)} {texts.h(m)} — {q} шт." for b, m, q in top]

    low = (
        await session.execute(
            select(Product.sku, Product.brand, Product.model, ProductSize.size, ProductSize.quantity - ProductSize.reserved)
            .join(ProductSize, ProductSize.product_id == Product.id)
            .where(Product.is_active.is_(True), ProductSize.quantity - ProductSize.reserved <= 1, ProductSize.quantity > 0)
            .order_by(Product.brand, Product.model, ProductSize.size)
            .limit(20)
        )
    ).all()
    if low:
        lines.append("\n<b>Закінчуються (доступно ≤ 1):</b>")
        lines += [f"{texts.h(sku)} {texts.h(b)} {texts.h(m)}, р. {texts.fmt_size(s)}: {a}" for sku, b, m, s, a in low]
    return "\n".join(lines)


async def stock_text(session: AsyncSession, product: Product | None = None) -> str:
    if product is not None:
        lines = [f"<b>{texts.h(product.sku)} · {texts.h(product.title)}</b>{texts.demo_mark(product)}",
                 f"Ціна: {texts.fmt_price(product.price)} · {'активний' if product.is_active else 'прихований'}", ""]
        if not product.sizes:
            lines.append("Розміри не задані.")
        for row in product.sizes:
            lines.append(f"р. {texts.fmt_size(row.size)}: на складі {row.quantity}, у резерві {row.reserved}, доступно {row.available}")
        return "\n".join(lines)
    products = (
        await session.scalars(select(Product).options(selectinload(Product.sizes)).order_by(Product.brand, Product.model))
    ).all()
    if not products:
        return "Каталог порожній."
    lines = ["<b>📊 Залишки</b> (розмір: доступно/на складі)"]
    for p in products:
        sizes = ", ".join(f"{texts.fmt_size(r.size)}: {r.available}/{r.quantity}" for r in p.sizes) or "—"
        mark = "" if p.is_active else " 🙈"
        lines.append(f"<b>{texts.h(p.sku)}</b> {texts.h(p.title)}{mark}\n   {sizes}")
    return "\n".join(lines)


CSV_COLUMNS = [
    "number", "created_at", "status", "brand", "model", "sku", "color", "size", "unit_price", "quantity",
    "total_amount", "customer_name", "contact", "delivery_service", "city", "delivery_point",
    "telegram_username", "telegram_id",
]


async def export_orders_csv(session: AsyncSession, days: int | None = None) -> bytes:
    stmt = select(Order).options(selectinload(Order.items)).order_by(Order.id)
    if days:
        stmt = stmt.where(Order.created_at >= utcnow() - timedelta(days=days))
    orders = (await session.scalars(stmt)).all()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for order in orders:
        for item in order.items:
            writer.writerow(
                [
                    order.number,
                    order.created_at.isoformat(timespec="seconds"),
                    order.status,
                    item.brand,
                    item.model,
                    item.sku,
                    item.color,
                    texts.fmt_size(item.size),
                    f"{item.unit_price:.2f}",
                    item.quantity,
                    f"{order.total_amount:.2f}",
                    _csv_safe(order.customer_name),
                    _csv_safe(order.contact),
                    texts.DELIVERY_LABELS.get(order.delivery_service or "", ""),
                    _csv_safe(order.city or ""),
                    _csv_safe(order.delivery_point or ""),
                    _csv_safe(order.username or ""),
                    order.telegram_id,
                ]
            )
    # UTF-8 with BOM so that Excel opens Cyrillic correctly
    return buffer.getvalue().encode("utf-8-sig")


def _csv_safe(value: str) -> str:
    """Prevent CSV/formula injection when the file is opened in Excel/Sheets."""
    if value and value[0] in "=+-@\t\r":
        return "'" + value
    return value
