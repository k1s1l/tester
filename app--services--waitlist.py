"""Restock notifications ("notify me when it is available")."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.database.models import Product, WaitlistRequest
from app.services.inventory import utcnow
from app.services.notifications import enqueue, enqueue_admins
from app.texts import fmt_size, h


async def add_request(
    session: AsyncSession,
    settings: Settings,
    *,
    telegram_id: int,
    product: Product | None,
    size: Decimal | None,
    query: str | None,
) -> bool:
    """Store a request (deduplicated). Returns False if an identical open request already exists."""
    stmt = select(WaitlistRequest.id).where(
        WaitlistRequest.telegram_id == telegram_id,
        WaitlistRequest.notified_at.is_(None),
        WaitlistRequest.product_id == product.id if product else WaitlistRequest.product_id.is_(None),
        WaitlistRequest.size == size if size is not None else WaitlistRequest.size.is_(None),
    )
    if await session.scalar(stmt.limit(1)):
        return False
    session.add(
        WaitlistRequest(
            telegram_id=telegram_id,
            product_id=product.id if product else None,
            size=size,
            query=(query or "")[:256] or None,
        )
    )
    what = product.title if product else (query or "товар")
    size_text = f", розмір {fmt_size(size)}" if size is not None else ""
    await enqueue_admins(
        session,
        settings,
        text=f"🔔 Клієнт (ID <code>{telegram_id}</code>) чекає на надходження: {h(what)}{size_text}",
        kind="admin_waitlist",
    )
    await session.flush()
    return True


async def notify_restock(session: AsyncSession, product: Product, size: Decimal) -> int:
    """Called when a size goes from 0 to >0 available. Queues messages for waiting clients."""
    requests = (
        await session.scalars(
            select(WaitlistRequest)
            .where(
                WaitlistRequest.product_id == product.id,
                WaitlistRequest.notified_at.is_(None),
                or_(WaitlistRequest.size.is_(None), WaitlistRequest.size == size),
            )
            .with_for_update(skip_locked=True)
        )
    ).all()
    for request in requests:
        await enqueue(
            session,
            chat_id=request.telegram_id,
            text=(
                f"🔔 Гарна новина! <b>{h(product.title)}</b> у розмірі {fmt_size(size)} знову в наявності. "
                "Напишіть мені, якщо бажаєте оформити замовлення."
            ),
            kind="client_restock",
        )
        request.notified_at = utcnow()
    await session.flush()
    return len(requests)
