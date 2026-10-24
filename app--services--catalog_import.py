"""Import/replace the catalog from JSON or CSV without changing bot code.

JSON: a list of objects matching ``ProductIn`` (see data/demo_catalog.json).
CSV:  one row per size with columns
      brand,model,sku,color,category,tags,price,description,is_active,is_demo,photo_urls,size,quantity
      (tags and photo_urls are separated by '|').
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Product, ProductPhoto
from app.schemas.catalog import ProductIn, SizeStockIn
from app.services import inventory


@dataclass(slots=True)
class ImportReport:
    created: int = 0
    updated: int = 0
    deactivated: int = 0


def load_file(path: str | Path) -> list[ProductIn]:
    path = Path(path)
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return [ProductIn.model_validate(item) for item in raw]
    if path.suffix.lower() == ".csv":
        return _from_csv(path)
    raise ValueError("Підтримуються лише файли .json та .csv")


def _from_csv(path: Path) -> list[ProductIn]:
    products: dict[str, dict] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            sku = row["sku"].strip().upper()
            item = products.setdefault(
                sku,
                {
                    "brand": row["brand"],
                    "model": row["model"],
                    "sku": sku,
                    "color": row.get("color", ""),
                    "category": row.get("category") or "lifestyle",
                    "tags": [t for t in (row.get("tags") or "").split("|") if t.strip()],
                    "price": row["price"],
                    "description": row.get("description", ""),
                    "is_active": (row.get("is_active") or "true").strip().lower() in ("1", "true", "yes", "так"),
                    "is_demo": (row.get("is_demo") or "false").strip().lower() in ("1", "true", "yes", "так"),
                    "photo_urls": [u for u in (row.get("photo_urls") or "").split("|") if u.strip()],
                    "sizes": [],
                },
            )
            if row.get("size"):
                item["sizes"].append({"size": row["size"], "quantity": int(row.get("quantity") or 0)})
    return [ProductIn.model_validate(item) for item in products.values()]


async def upsert_product(session: AsyncSession, data: ProductIn, *, actor: int | None = None) -> tuple[Product, bool]:
    product = await session.scalar(
        select(Product).options(selectinload(Product.sizes), selectinload(Product.photos)).where(Product.sku == data.sku)
    )
    created = product is None
    if product is None:
        product = Product(sku=data.sku, sizes=[], photos=[])
        session.add(product)
    product.brand = data.brand
    product.model = data.model
    product.color = data.color
    product.category = data.category
    product.tags = list(data.tags)
    product.price = data.price
    product.description = data.description
    product.is_active = data.is_active
    product.is_demo = data.is_demo
    await session.flush()

    await set_sizes(session, product, data.sizes, actor=actor, comment="catalog import")
    existing_urls = {p.url for p in product.photos if p.url}
    for position, url in enumerate(data.photo_urls):
        if url not in existing_urls:
            session.add(ProductPhoto(product_id=product.id, url=url, position=position))
    await session.flush()
    return product, created


async def set_sizes(
    session: AsyncSession, product: Product, sizes: Iterable[SizeStockIn], *, actor: int | None, comment: str
) -> list[tuple[SizeStockIn, int]]:
    """Set quantities for the given sizes. Returns (row, previous_available) pairs."""
    changes = []
    for row in sizes:
        size_row, previous = await inventory.set_quantity(
            session, product_id=product.id, size=row.size, quantity=row.quantity, actor=actor, comment=comment
        )
        changes.append((row, previous))
    return changes


async def import_catalog(
    session: AsyncSession, items: Iterable[ProductIn], *, deactivate_missing: bool = False
) -> ImportReport:
    report = ImportReport()
    seen: list[str] = []
    for item in items:
        _, created = await upsert_product(session, item)
        seen.append(item.sku)
        if created:
            report.created += 1
        else:
            report.updated += 1
    if deactivate_missing:
        result = await session.execute(
            update(Product).where(Product.sku.not_in(seen), Product.is_active.is_(True)).values(is_active=False)
        )
        report.deactivated = result.rowcount or 0
    await session.flush()
    return report
