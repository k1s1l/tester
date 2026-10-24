"""Validated catalog input (admin forms, JSON/CSV import)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, field_validator

ALLOWED_CATEGORIES = ("lifestyle", "running", "basketball", "training", "walking")
_SKU_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def parse_size(value: str | float | int | Decimal, *, min_size: float = 30, max_size: float = 52) -> Decimal:
    """Parse '42', '42,5', '42.5', 'EU 43' into Decimal with one decimal digit. Only whole and half sizes."""
    raw = str(value).strip().lower().replace(",", ".")
    raw = re.sub(r"^(eu|eur|розмір|размер|р\.?)\s*", "", raw)
    try:
        size = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"Некоректний розмір: {value}") from exc
    if size * 2 != (size * 2).to_integral_value():
        raise ValueError(f"Некоректний розмір: {value}")
    if not (Decimal(str(min_size)) <= size <= Decimal(str(max_size))):
        raise ValueError(f"Розмір поза допустимим діапазоном: {value}")
    return size.quantize(Decimal("0.1"))


class SizeStockIn(BaseModel):
    size: Decimal
    quantity: int = Field(ge=0, le=100_000)

    @field_validator("size", mode="before")
    @classmethod
    def _size(cls, value: object) -> Decimal:
        return parse_size(value)  # type: ignore[arg-type]


class ProductIn(BaseModel):
    brand: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    sku: str
    color: str = Field(default="", max_length=128)
    category: str = "lifestyle"
    tags: list[str] = Field(default_factory=list)
    price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)
    description: str = Field(default="", max_length=4000)
    is_active: bool = True
    is_demo: bool = False
    sizes: list[SizeStockIn] = Field(default_factory=list)
    photo_urls: list[str] = Field(default_factory=list)

    @field_validator("brand", "model", "color", "description")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    @field_validator("sku")
    @classmethod
    def _sku(cls, value: str) -> str:
        value = value.strip().upper()
        if not _SKU_RE.match(value):
            raise ValueError("Артикул: лише латиниця, цифри, '.', '_' або '-' (до 64 символів)")
        return value

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in ALLOWED_CATEGORIES:
            raise ValueError(f"Категорія має бути однією з: {', '.join(ALLOWED_CATEGORIES)}")
        return value

    @field_validator("photo_urls")
    @classmethod
    def _urls(cls, value: list[str]) -> list[str]:
        for url in value:
            if not url.startswith("https://"):
                raise ValueError("Посилання на фото мають починатися з https://")
        return value


def parse_sizes_spec(spec: str) -> list[SizeStockIn]:
    """Parse admin input like '42:3, 43:1, 44.5:0' into validated size rows."""
    result: dict[Decimal, SizeStockIn] = {}
    for chunk in re.split(r"[,;\n]+", spec):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.fullmatch(r"([\d.,]+)\s*[:=\-]\s*(\d+)", chunk)
        if not match:
            raise ValueError(f"Не вдалося розпізнати «{chunk}». Формат: 42:3, 43:1")
        row = SizeStockIn(size=match.group(1), quantity=int(match.group(2)))
        result[row.size] = row
    if not result:
        raise ValueError("Не вказано жодного розміру")
    return list(result.values())
