from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from app import texts
from app.config import Settings
from app.services.rate_limit import SlidingWindowLimiter


class ThrottlingMiddleware(BaseMiddleware):
    """Per-user anti-flood. Admins are not limited."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._limiter = SlidingWindowLimiter(settings.rate_limit_per_minute, 60)
        self._warned: dict[int, float] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or self._settings.is_admin(user.id) or self._limiter.allow(user.id):
            return await handler(event, data)
        now = time.monotonic()
        if len(self._warned) > 10_000:
            self._warned.clear()
        if now - self._warned.get(user.id, 0) > 60:
            self._warned[user.id] = now
            if isinstance(event, Message):
                await event.answer(texts.RATE_LIMITED)
        if isinstance(event, CallbackQuery):
            await event.answer(texts.RATE_LIMITED, show_alert=False)
        return None
