"""Catalog queries, fuzzy product matching and alternative suggestions.

The catalog of an online sneaker shop is small (hundreds to a few thousand models),
so active products are loaded with their sizes and matched in Python with RapidFuzz.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from rapidfuzz import fuzz
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database.models import Product, ProductSize

MATCH_THRESHOLD = 80
BRAND_THRESHOLD = 80

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie", "ж": "zh", "з": "z",
    "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p",
    "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ь": "", "ю": "iu", "я": "ia", "ё": "e", "ы": "y", "э": "e", "ъ": "", "'": "", "’": "",
}

# Common spoken/cyrillic names of brands -> catalog spelling
BRAND_ALIASES = {
    "найк": "nike", "найки": "nike", "найкі": "nike",
    "адидас": "adidas", "адідас": "adidas",
    "нью беланс": "new balance", "нью баланс": "new balance", "nb": "new balance",
    "асикс": "asics", "асікс": "asics",
    "пума": "puma", "рибок": "reebok", "рібок": "reebok", "салпмон": "salomon", "саломон": "salomon",
    "джордан": "jordan", "конверс": "converse", "ванс": "vans",
}

COLOR_SYNONYMS = {
    "white": "білий", "black": "чорний", "grey": "сірий", "gray": "сірий", "red": "червоний",
    "blue": "синій", "navy": "синій", "green": "зелений", "beige": "бежевий", "silver": "срібний",
    "pink": "рожевий", "brown": "коричневий", "yellow": "жовтий", "orange": "помаранчевий",
    "белый": "білий", "черный": "чорний", "чёрный": "чорний", "серый": "сірий", "красный": "червоний",
    "синий": "синій", "зеленый": "зелений", "бежевый": "бежевий", "серебристый": "срібний",
}


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = "".join(_TRANSLIT.get(ch, ch) for ch in text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def canonical_brand(brand: str) -> str:
    key = " ".join(brand.lower().split())
    return BRAND_ALIASES.get(key, normalize(brand))


def _digits(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def _color_stems(color: str) -> set[str]:
    words = re.findall(r"[a-zа-яіїєґё]+", color.lower())
    stems = set()
    for word in words:
        word = COLOR_SYNONYMS.get(word, word)
        if len(word) >= 3:
            stems.add(word[:3])
    return stems


def color_matches(product: Product, color: str | None) -> bool:
    if not color:
        return False
    return bool(_color_stems(product.color) & _color_stems(color))


def brand_matches(product: Product, brand: str | None) -> bool:
    if not brand:
        return False
    wanted = canonical_brand(brand)
    have = normalize(product.brand)
    return wanted == have or fuzz.ratio(wanted, have) >= BRAND_THRESHOLD


def model_score(product: Product, model: str, brand: str | None = None) -> float:
    query = normalize(model)
    # Strip brand words if the user wrote "nike vomero 5" as a model
    for token in normalize(product.brand).split():
        query = re.sub(rf"\b{re.escape(token)}\b", " ", query)
    if brand:
        for token in canonical_brand(brand).split():
            query = re.sub(rf"\b{re.escape(token)}\b", " ", query)
    query = " ".join(query.split())
    if not query:
        return 0.0
    target = normalize(product.model)
    score = max(
        fuzz.token_sort_ratio(query, target),
        fuzz.ratio(query.replace(" ", ""), target.replace(" ", "")),
    )
    query_tokens, target_tokens = set(query.split()), set(target.split())
    if query_tokens and query_tokens <= target_tokens:
        score = max(score, 95.0)
    q_digits, t_digits = _digits(query), _digits(target)
    if q_digits and t_digits and not (q_digits & t_digits):
        score *= 0.6  # "Vomero 18" must not match "Vomero 5"
    if brand and not brand_matches(product, brand):
        score *= 0.7
    return float(score)


def match_products(
    products: Iterable[Product], *, brand: str | None, model: str | None, limit: int = 5
) -> list[Product]:
    """Products whose model name matches the query (best first)."""
    if not model:
        return []
    scored = [(model_score(p, model, brand), p) for p in products]
    scored = [(s, p) for s, p in scored if s >= MATCH_THRESHOLD]
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return []
    best = scored[0][0]
    # Keep only near-best results so "Vomero 5" does not return a long tail.
    return [p for s, p in scored if s >= best - 5][:limit]


def size_row(product: Product, size: Decimal) -> ProductSize | None:
    for row in product.sizes:
        if row.size == size:
            return row
    return None


def has_size(product: Product, size: Decimal | None) -> bool:
    if size is None:
        return any(row.available > 0 for row in product.sizes)
    row = size_row(product, size)
    return row is not None and row.available > 0


@dataclass(slots=True)
class Criteria:
    brand: str | None = None
    color: str | None = None
    max_price: Decimal | None = None
    purpose: str | None = None
    size: Decimal | None = None


def filter_products(products: Iterable[Product], criteria: Criteria, limit: int = 5) -> list[Product]:
    """Products satisfying every given criterion (used when the client gives no model name)."""
    result = []
    for product in products:
        if criteria.brand and not brand_matches(product, criteria.brand):
            continue
        if criteria.color and not color_matches(product, criteria.color):
            continue
        if criteria.max_price is not None and product.price > criteria.max_price:
            continue
        if criteria.purpose and criteria.purpose != "any" and product.category != criteria.purpose:
            continue
        if not has_size(product, criteria.size):
            continue
        result.append(product)
    result.sort(key=lambda p: (-sum(r.available for r in p.sizes), p.price))
    return result[:limit]


def find_alternatives(
    products: Iterable[Product],
    *,
    reference: Product | None,
    criteria: Criteria,
    exclude_ids: Iterable[int] = (),
    limit: int = 3,
) -> list[Product]:
    """Up to ``limit`` similar products that are really in stock (in the requested size, if any)."""
    excluded = set(exclude_ids)
    if reference is not None:
        excluded.add(reference.id)
    ref_price = reference.price if reference is not None else criteria.max_price
    ref_tags = set(reference.tags) if reference is not None else set()
    scored: list[tuple[float, Product]] = []
    for product in products:
        if product.id in excluded or not product.is_active or not has_size(product, criteria.size):
            continue
        if criteria.max_price is not None and product.price > criteria.max_price * Decimal("1.15"):
            continue
        score = 0.0
        if reference is not None:
            if product.brand == reference.brand:
                score += 3
            if product.category == reference.category:
                score += 3
            if _color_stems(product.color) & _color_stems(reference.color):
                score += 2
            score += len(ref_tags & set(product.tags))
        if criteria.brand and brand_matches(product, criteria.brand):
            score += 3
        if criteria.color and color_matches(product, criteria.color):
            score += 2
        if criteria.purpose and criteria.purpose != "any" and product.category == criteria.purpose:
            score += 2
        if ref_price:
            diff = abs(float(product.price) - float(ref_price)) / float(ref_price)
            score += max(0.0, 3 * (1 - diff))
        scored.append((score, product))
    has_signal = reference is not None or any(
        [criteria.brand, criteria.color, criteria.purpose, criteria.max_price, criteria.size]
    )
    if not has_signal:
        return []
    scored.sort(key=lambda item: (item[0], -float(item[1].price)), reverse=True)
    return [product for _, product in scored[:limit]]


def popular(products: Iterable[Product], limit: int = 5) -> list[Product]:
    in_stock = [p for p in products if has_size(p, None)]
    in_stock.sort(key=lambda p: -sum(r.available for r in p.sizes))
    return in_stock[:limit]


def brand_products(products: Iterable[Product], brand: str, size: Decimal | None = None) -> list[Product]:
    return [p for p in products if brand_matches(p, brand) and (size is None or has_size(p, size))]


def catalog_names(products: Sequence[Product], limit: int = 300) -> list[str]:
    return [f"{p.brand} — {p.model} ({p.color})" for p in products[:limit]]


# --------------------------------------------------------------------- DB helpers

def _with_relations():
    return (selectinload(Product.sizes), selectinload(Product.photos))


async def load_active_products(session: AsyncSession) -> list[Product]:
    result = await session.scalars(
        select(Product).options(*_with_relations()).where(Product.is_active.is_(True)).order_by(Product.brand, Product.model)
    )
    return list(result.all())


async def get_product(session: AsyncSession, product_id: int) -> Product | None:
    return await session.scalar(select(Product).options(*_with_relations()).where(Product.id == product_id))


async def get_product_by_sku(session: AsyncSession, sku: str) -> Product | None:
    return await session.scalar(select(Product).options(*_with_relations()).where(Product.sku == sku.strip().upper()))


async def search_admin(session: AsyncSession, query: str, *, offset: int, limit: int) -> list[Product]:
    stmt = select(Product).options(*_with_relations()).order_by(Product.brand, Product.model, Product.id)
    if query:
        pattern = f"%{query}%"  # bound parameter: safe from SQL injection
        stmt = stmt.where(
            or_(Product.brand.ilike(pattern), Product.model.ilike(pattern), Product.sku.ilike(pattern), Product.color.ilike(pattern))
        )
    return list((await session.scalars(stmt.offset(offset).limit(limit))).all())
