"""PostgreSQL-backed aiogram FSM storage.

Keeps dialog state (order checkout step, selected product, short history) across restarts
and redeploys without requiring Redis.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import (
    BaseStorage,
    DefaultKeyBuilder,
    KeyBuilder,
    StateType,
    StorageKey,
)
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.database.models import FsmRecord
from app.database.session import SessionFactory


class PostgresStorage(BaseStorage):
    def __init__(self, session_factory: SessionFactory, key_builder: KeyBuilder | None = None) -> None:
        self._session_factory = session_factory
        self._key_builder = key_builder or DefaultKeyBuilder(with_destiny=True)

    def _key(self, key: StorageKey) -> str:
        return self._key_builder.build(key)

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        value = state.state if isinstance(state, State) else state
        async with self._session_factory() as session, session.begin():
            stmt = insert(FsmRecord).values(key=self._key(key), state=value, data={})
            stmt = stmt.on_conflict_do_update(
                index_elements=[FsmRecord.key], set_={"state": value, "updated_at": stmt.excluded.updated_at}
            )
            await session.execute(stmt)

    async def get_state(self, key: StorageKey) -> str | None:
        async with self._session_factory() as session:
            return await session.scalar(select(FsmRecord.state).where(FsmRecord.key == self._key(key)))

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        payload = dict(data)
        async with self._session_factory() as session, session.begin():
            stmt = insert(FsmRecord).values(key=self._key(key), state=None, data=payload)
            stmt = stmt.on_conflict_do_update(
                index_elements=[FsmRecord.key], set_={"data": payload, "updated_at": stmt.excluded.updated_at}
            )
            await session.execute(stmt)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        async with self._session_factory() as session:
            data = await session.scalar(select(FsmRecord.data).where(FsmRecord.key == self._key(key)))
        return dict(data or {})

    async def delete(self, key: StorageKey) -> None:
        async with self._session_factory() as session, session.begin():
            await session.execute(delete(FsmRecord).where(FsmRecord.key == self._key(key)))

    async def close(self) -> None:
        return None
