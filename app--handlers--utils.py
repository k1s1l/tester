from __future__ import annotations

import logging

from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, User

from app.config import Settings
from app.services.assistant import BotReply, DialogContext, UserInfo

log = logging.getLogger(__name__)

CAPTION_LIMIT = 1024


def user_info(user: User) -> UserInfo:
    return UserInfo(id=user.id, username=user.username, full_name=user.full_name)


async def load_ctx(state: FSMContext, settings: Settings) -> DialogContext:
    return DialogContext.from_state(await state.get_data(), settings.dialog_ttl_hours)


async def save_ctx(state: FSMContext, ctx: DialogContext) -> None:
    await state.update_data(ctx.to_state())


async def send_reply(message: Message, reply: BotReply) -> None:
    if reply.photo and len(reply.text) <= CAPTION_LIMIT:
        try:
            await message.answer_photo(reply.photo, caption=reply.text, reply_markup=reply.keyboard)
            return
        except TelegramBadRequest as exc:  # broken file id / url: fall back to text
            log.warning("Could not send product photo: %s", exc)
    await message.answer(reply.text, reply_markup=reply.keyboard)
