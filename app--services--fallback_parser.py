"""Rule-based message parser used when Claude is unavailable (outage, rate limit, no API key).

It produces the same ``MessageAnalysis`` structure, so the rest of the bot keeps working:
catalog search, stock checks and checkout do not depend on the AI provider.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.database.models import Product
from app.schemas.ai import MessageAnalysis
from app.services.catalog import BRAND_ALIASES, COLOR_SYNONYMS, normalize

_SIZE_RE = re.compile(r"(?<![\d.,])(3[0-9]|4[0-9]|5[0-2])(?:[.,](5))?(?![\d])")
_PRICE_RE = re.compile(r"(?:до|до\s+|<|max|бюджет\D{0,10})\s*(\d[\d\s]{2,6})\s*(?:грн|uah|₴)?", re.IGNORECASE)
_OPTION_RE = re.compile(r"\b(перш\w*|друг\w*|трет\w*|четверт\w*|п'?ят\w*|№\s*\d|номер\s*\d)\b", re.IGNORECASE)
_ORDINALS = {"перш": 1, "друг": 2, "трет": 3, "четв": 4, "пят": 5, "п'ят": 5}
_UA_COLORS = ("біл", "чорн", "сір", "червон", "син", "зелен", "беж", "срібл", "рожев", "коричн", "жовт", "помаранч")


def _has(text: str, *words: str) -> bool:
    return any(word in text for word in words)


def parse_message(text: str, products: Sequence[Product]) -> MessageAnalysis:
    lowered = text.lower()
    norm = normalize(text)

    brand = None
    for alias, canonical in BRAND_ALIASES.items():
        if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered):
            brand = canonical.title() if canonical != "asics" else "ASICS"
    for product in products:
        if normalize(product.brand) and re.search(rf"\b{re.escape(normalize(product.brand))}\b", norm):
            brand = product.brand

    model = None
    best_len = 0
    for product in products:
        tokens = normalize(product.model).split()
        if tokens and all(re.search(rf"\b{re.escape(t)}\b", norm) for t in tokens) and len(tokens) > best_len:
            model, best_len = product.model, len(tokens)
            brand = brand or product.brand
    if model is None:
        compact = norm.replace(" ", "")
        for product in products:
            key = normalize(product.model).replace(" ", "")
            if len(key) >= 4 and key in compact:
                model, brand = product.model, brand or product.brand
                break

    size = None
    size_text = re.sub(r"\d{3,}", " ", lowered)
    if model:
        # digits that belong to the model name ("Kayano 31") are not a size
        for digits in re.findall(r"\d+", model):
            size_text = re.sub(rf"(?<![\d.,]){digits}(?![\d.,])", " ", size_text, count=1)
    if match := _SIZE_RE.search(size_text):
        size = match.group(1) + (".5" if match.group(2) else "")

    color = None
    for word in re.findall(r"[a-zа-яіїєґё']+", lowered):
        if word in COLOR_SYNONYMS:
            color = COLOR_SYNONYMS[word]
            break
        if any(word.startswith(stem) for stem in _UA_COLORS):
            color = word
            break

    max_price = None
    if match := _PRICE_RE.search(lowered):
        max_price = int(re.sub(r"\s", "", match.group(1)))

    selected = None
    if match := _OPTION_RE.search(lowered):
        token = match.group(1).lower()
        digits = re.findall(r"\d", token)
        if digits:
            selected = int(digits[0])
        else:
            for prefix, value in _ORDINALS.items():
                if token.startswith(prefix):
                    selected = value

    purpose = None
    if _has(lowered, "біг", "бег", "running"):
        purpose = "running"
    elif _has(lowered, "баскет", "basket"):
        purpose = "basketball"

    if _has(lowered, "скасу", "відмін", "отмен", "не треба", "не потрібно"):
        intent = "cancel"
    elif _has(lowered, "замов", "оформ", "куп", "беру", "заказ", "придба"):
        intent = "start_order"
    elif _has(lowered, "мої замовлення", "статус замов", "де моє замовлення", "мой заказ"):
        intent = "order_status"
    elif _has(lowered, "схож", "альтернатив", "щось інше", "інші модел", "похож"):
        intent = "alternatives"
    elif selected and not model:
        intent = "choose_option"
    elif model or brand or color or max_price or purpose:
        intent = "search"
    elif size:
        intent = "check_size"
    elif _has(lowered, "привіт", "вітаю", "добр", "hello", "hi", "здравств", "привет"):
        intent = "greeting"
    else:
        intent = "smalltalk"

    return MessageAnalysis(
        intent=intent,  # type: ignore[arg-type]
        brand=brand,
        model=model,
        size=size,
        color=color,
        max_price=max_price,
        purpose=purpose,  # type: ignore[arg-type]
        selected_option=selected,
        reply="",
    )
