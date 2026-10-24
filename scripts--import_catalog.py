"""Import or update the catalog from a JSON/CSV file.

Usage:
    python -m scripts.import_catalog data/demo_catalog.json
    python -m scripts.import_catalog my_catalog.csv --deactivate-missing

Products are matched by SKU: existing ones are updated, new ones are created. Quantities in the file
replace the physical quantity per size (a size cannot go below its currently reserved amount).
--deactivate-missing hides products whose SKU is not in the file (e.g. to retire the demo catalog).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config import get_settings
from app.database.session import create_engine, create_session_factory
from app.services.catalog_import import import_catalog, load_file


async def run(path: str, deactivate_missing: bool) -> None:
    settings = get_settings()
    items = load_file(path)
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            report = await import_catalog(session, items, deactivate_missing=deactivate_missing)
    finally:
        await engine.dispose()
    print(f"Created: {report.created}, updated: {report.updated}, deactivated: {report.deactivated}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", help="JSON or CSV file")
    parser.add_argument("--deactivate-missing", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.path, args.deactivate_missing))
    except Exception as exc:  # show a readable error instead of a traceback for data errors
        print(f"Import failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
