"""Test doubles for Claude and Telegram."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import SendDocument, SendMessage, SendPhoto, TelegramMethod
from aiogram.types import Chat, Message

from app.schemas.ai import MessageAnalysis, PhotoAnalysis
from app.services.ai import AIUnavailable


class FakeAI:
    """Stands in for ClaudeService. Configure ``message_result`` / ``photo_result`` per test."""

    def __init__(self) -> None:
        self.enabled = True
        self.message_result: MessageAnalysis | Exception | None = None
        self.photo_result: PhotoAnalysis | Exception | None = None
        self.calls: list[dict[str, Any]] = []

    async def analyze_message(self, **kwargs: Any) -> MessageAnalysis:
        self.calls.append(kwargs)
        result = self.message_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AIUnavailable("not configured")
        return result

    async def analyze_photo(self, **kwargs: Any) -> PhotoAnalysis:
        self.calls.append(kwargs)
        result = self.photo_result
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise AIUnavailable("not configured")
        return result

    async def close(self) -> None:
        return None


def analysis(intent: str = "search", **kwargs: Any) -> MessageAnalysis:
    kwargs.setdefault("reply", "")
    return MessageAnalysis(intent=intent, **kwargs)  # type: ignore[arg-type]


class MockedSession(BaseSession):
    """Records every Bot API call instead of sending it."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod[Any]] = []
        self.fail_with: Exception | None = None

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if self.fail_with is not None:
            raise self.fail_with
        self.requests.append(method)
        if isinstance(method, (SendMessage, SendPhoto, SendDocument)):
            return Message(
                message_id=len(self.requests),
                date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        return True

    async def stream_content(self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
                             chunk_size: int = 65536, raise_for_status: bool = True) -> AsyncGenerator[bytes, None]:
        yield b""

    async def close(self) -> None:
        return None

    def sent_texts(self, chat_id: int | None = None) -> list[str]:
        return [
            r.text if isinstance(r, SendMessage) else (r.caption or "")
            for r in self.requests
            if isinstance(r, (SendMessage, SendPhoto)) and (chat_id is None or int(r.chat_id) == chat_id)
        ]

    def clear(self) -> None:
        self.requests.clear()
