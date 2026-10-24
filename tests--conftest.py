"""Shared fixtures. Tests run against a real PostgreSQL database (TEST_DATABASE_URL).

Default: postgresql+asyncpg://postgres:postgres@localhost:5432/sneaker_bot_test
With Docker:  docker compose up -d db && docker compose run --rm bot pytest
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.database.base import Base
from app.database.session import create_session_factory
from app.services.assistant import DialogContext, SalesAssistant, UserInfo
from app.services.catalog_import import import_catalog, load_file
from tests.fakes import FakeAI

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/sneaker_bot_test"
)
DEMO = Path(__file__).resolve().parent.parent / "data" / "demo_catalog.json"
ADMIN_ID = 111
ADMIN_CHAT = -100500
CLIENT_ID = 555


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        bot_token="123456:TEST-TOKEN",
        database_url=TEST_DATABASE_URL,
        admin_ids=str(ADMIN_ID),
        admin_chat_id=ADMIN_CHAT,
        anthropic_api_key=None,
        rate_limit_per_minute=1000,
        reservation_ttl_minutes=60,
    )


@pytest.fixture(scope="session")
async def engine(settings: Settings):
    engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=10)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture(autouse=True)
async def clean_db(engine, session_factory):
    tables = ", ".join(t.name for t in reversed(Base.metadata.sorted_tables))
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    async with session_factory() as session, session.begin():
        await import_catalog(session, load_file(DEMO))
    yield


@pytest.fixture
def fake_ai() -> FakeAI:
    return FakeAI()


@pytest.fixture
def assistant(settings, session_factory, fake_ai) -> SalesAssistant:
    return SalesAssistant(settings, session_factory, fake_ai)  # type: ignore[arg-type]


@pytest.fixture
def user() -> UserInfo:
    return UserInfo(id=CLIENT_ID, username="client", full_name="Test Client")


@pytest.fixture
def ctx() -> DialogContext:
    return DialogContext()
