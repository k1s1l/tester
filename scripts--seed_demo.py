"""Fill the database with the DEMO catalog (data/demo_catalog.json).

All demo products are flagged is_demo=true and shown to clients as "демонстраційний товар".
They are NOT the real assortment of any shop.

Usage:
    python -m scripts.seed_demo             # create/update demo products (resets demo quantities)
    python -m scripts.seed_demo --if-empty  # only if the catalog is empty (used by scripts/start.sh)
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from sqlalchemy import func, select

from app.config import get_settings
from app.database.models import Product
from app.database.session import create_engine, create_session_factory
from scripts.import_catalog import run

DEMO_FILE = Path(__file__).resolve().parent.parent / "data" / "demo_catalog.json"


async def catalog_is_empty() -> bool:
    engine = create_engine(get_settings())
    try:
        async with create_session_factory(engine)() as session:
            return not await session.scalar(select(func.count(Product.id)))
    finally:
        await engine.dispose()


async def main(if_empty: bool) -> None:
    if if_empty and not await catalog_is_empty():
        print("Catalog is not empty: demo data is not loaded.")
        return
    await run(str(DEMO_FILE), deactivate_missing=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--if-empty", action="store_true")
    asyncio.run(main(parser.parse_args().if_empty))
