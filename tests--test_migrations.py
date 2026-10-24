"""Alembic migrations create exactly the schema described by the ORM models."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import TEST_DATABASE_URL

ROOT = Path(__file__).resolve().parent.parent
DB_NAME = "sneaker_bot_migrations_test"


def _dsn(database: str) -> str:
    base = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    return base.rsplit("/", 1)[0] + "/" + database


async def test_upgrade_downgrade_and_no_drift():
    try:
        admin = await asyncpg.connect(_dsn("postgres"))
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"cannot connect to create a scratch database: {exc}")
    try:
        await admin.execute(f'DROP DATABASE IF EXISTS "{DB_NAME}"')
        await admin.execute(f'CREATE DATABASE "{DB_NAME}"')
    finally:
        await admin.close()

    env = {**os.environ, "DATABASE_URL": _dsn(DB_NAME)}

    def alembic(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120
        )

    for step in (("upgrade", "head"), ("downgrade", "base"), ("upgrade", "head"), ("check",)):
        result = alembic(*step)
        assert result.returncode == 0, f"alembic {' '.join(step)} failed:\n{result.stdout}\n{result.stderr}"
