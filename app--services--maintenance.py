"""Background jobs: reservation expiry, notification retries, personal data retention."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.database.models import Customer, FsmRecord, Order
from app.database.session import SessionFactory
from app.services.inventory import utcnow
from app.services.notifications import NotificationSender
from app.services.order_rules import FINAL_STATUSES
from app.services.orders import expire_overdue

log = logging.getLogger(__name__)


async def purge_personal_data(session_factory: SessionFactory, settings: Settings) -> int:
    """Anonymise closed orders and idle customer profiles older than DATA_RETENTION_DAYS,
    and drop idle dialog contexts older than DIALOG_TTL_HOURS (FSM rows without an active state)."""
    cutoff = utcnow() - timedelta(days=settings.data_retention_days)
    async with session_factory() as session, session.begin():
        result = await session.execute(
            update(Order)
            .where(Order.status.in_(FINAL_STATUSES), Order.updated_at < cutoff, Order.anonymized_at.is_(None))
            .values(customer_name="—", contact="—", username=None, anonymized_at=utcnow())
        )
        orders = result.rowcount or 0
        await session.execute(
            update(Customer)
            .where(Customer.last_seen_at < cutoff)
            .values(full_name=None, contact=None, username=None, telegram_name=None)
        )
        await session.execute(
            delete(FsmRecord).where(
                FsmRecord.updated_at < utcnow() - timedelta(hours=settings.dialog_ttl_hours),
                FsmRecord.state.is_(None),
            )
        )
    if orders:
        log.info("Anonymised %s old orders", orders)
    return orders


class BackgroundWorker:
    def __init__(self, session_factory: SessionFactory, settings: Settings, sender: NotificationSender) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._sender = sender
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="background-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def run_once(self, *, purge: bool = False) -> None:
        jobs = [
            ("expire reservations", expire_overdue(self._session_factory, self._settings)),
            ("deliver notifications", self._sender.deliver_due()),
        ]
        if purge:
            jobs.append(("purge personal data", purge_personal_data(self._session_factory, self._settings)))
        for name, job in jobs:
            try:
                await job
            except SQLAlchemyError:
                log.exception("Background job '%s' failed (database error)", name)
            except Exception:
                log.exception("Background job '%s' failed", name)

    async def _run(self) -> None:
        iteration = 0
        while not self._stop.is_set():
            # Purge once an hour (every 120 iterations of 30 s).
            await self.run_once(purge=iteration % max(1, 3600 // self._settings.worker_interval_seconds) == 0)
            iteration += 1
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._settings.worker_interval_seconds)
