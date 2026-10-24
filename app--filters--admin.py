from __future__ import annotations

from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message

from app.config import Settings


class IsAdmin(BaseFilter):
    """Passes only for Telegram user ids listed in ADMIN_IDS."""

    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        user = event.from_user
        return user is not None and settings.is_admin(user.id)
