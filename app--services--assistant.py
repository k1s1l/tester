"""Sales dialog orchestration.

Flow for every client message:
    1. Claude (or the rule-based fallback) turns free text into a validated ``MessageAnalysis``.
    2. Python resolves products in the catalog and checks real stock in PostgreSQL.
    3. The reply is assembled from Claude's short conversational phrase + facts rendered from the DB.

This module is independent of aiogram handlers so it can be unit-tested directly.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import texts
from app.config import Settings
from app.database.models import Order, Product
from app.database.session import SessionFactory
from app.keyboards import client as kb
from app.schemas.ai import MessageAnalysis
from app.schemas.catalog import parse_size
from app.schemas.order import mask_personal_data
from app.services import catalog
from app.services.ai import AIUnavailable, ClaudeService
from app.services.catalog import Criteria
from app.services.fallback_parser import parse_message
from app.services.rate_limit import SlidingWindowLimiter

log = logging.getLogger(__name__)

PHOTO_CONFIDENCE_THRESHOLD = 0.6
MAX_TEXT_LENGTH = 1000

# Claude's phrase is dropped if it tries to state facts that only the database may state.
_FACT_PATTERNS = re.compile(
    r"(\d[\d\s]*\s*(грн|uah|₴|гривень))|в наявності|є в наявн|немає в наявн|залишил|на складі|"
    r"доставк|знижк|оплачен",
    re.IGNORECASE,
)


@dataclass(slots=True)
class UserInfo:
    id: int
    username: str | None = None
    full_name: str | None = None


@dataclass(slots=True)
class CheckoutRequest:
    product_id: int
    size: Decimal


@dataclass(slots=True)
class BotReply:
    text: str
    keyboard: InlineKeyboardMarkup | None = None
    photo: str | None = None
    checkout: CheckoutRequest | None = None


@dataclass
class DialogContext:
    history: list[dict[str, str]] = field(default_factory=list)
    last_shown: list[int] = field(default_factory=list)
    product_id: int | None = None
    size: str | None = None
    updated_at: float = 0.0

    @classmethod
    def from_state(cls, data: dict[str, Any], ttl_hours: int) -> DialogContext:
        raw = data.get("dialog") or {}
        ctx = cls(
            history=list(raw.get("history") or []),
            last_shown=[int(x) for x in raw.get("last_shown") or []],
            product_id=raw.get("product_id"),
            size=raw.get("size"),
            updated_at=float(raw.get("updated_at") or 0),
        )
        if ctx.updated_at and time.time() - ctx.updated_at > ttl_hours * 3600:
            return cls()  # stale dialog: forget everything
        return ctx

    def to_state(self) -> dict[str, Any]:
        self.updated_at = time.time()
        return {"dialog": asdict(self)}

    def push(self, user_text: str, bot_text: str, limit: int) -> None:
        self.history.append({"role": "user", "content": f"<customer_message>\n{user_text[:500]}\n</customer_message>"})
        self.history.append({"role": "assistant", "content": _plain(bot_text)[:600]})
        self.history = self.history[-limit:] if limit > 0 else []

    def size_decimal(self) -> Decimal | None:
        try:
            return Decimal(self.size) if self.size else None
        except Exception:
            return None


def _plain(html_text: str) -> str:
    return re.sub(r"<[^>]+>", "", html_text)


def _safe_phrase(phrase: str | None) -> str:
    if not phrase:
        return ""
    phrase = phrase.strip()
    if len(phrase) > 400 or _FACT_PATTERNS.search(phrase):
        return ""
    return texts.h(phrase)


def _join(*parts: str) -> str:
    return "\n\n".join(p for p in parts if p)


class SalesAssistant:
    def __init__(self, settings: Settings, session_factory: SessionFactory, ai: ClaudeService) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._ai = ai
        self._ai_limiter = SlidingWindowLimiter(settings.ai_requests_per_user_per_hour, 3600)

    # ------------------------------------------------------------------ helpers

    def _valid_size(self, raw: str | None) -> Decimal | None:
        if not raw:
            return None
        try:
            return parse_size(raw, min_size=self._settings.min_size, max_size=self._settings.max_size)
        except ValueError:
            return None

    async def _products(self) -> list[Product]:
        async with self._session_factory() as session:
            return await catalog.load_active_products(session)

    def _dialog_state(self, ctx: DialogContext, products: Sequence[Product]) -> str:
        by_id = {p.id: p for p in products}
        lines = []
        if ctx.product_id and ctx.product_id in by_id:
            lines.append(f"selected_product: {by_id[ctx.product_id].title}")
        if ctx.size:
            lines.append(f"selected_size: {ctx.size}")
        if ctx.last_shown:
            shown = [f"{i}) {by_id[pid].title}" for i, pid in enumerate(ctx.last_shown, 1) if pid in by_id]
            lines.append("last_shown_list: " + "; ".join(shown))
        return "\n".join(lines) or "empty"

    async def _analyze(self, user: UserInfo, text: str, ctx: DialogContext, products: Sequence[Product]) -> tuple[MessageAnalysis, bool]:
        if self._ai.enabled and self._ai_limiter.allow(user.id):
            try:
                analysis = await self._ai.analyze_message(
                    text=text,
                    history=ctx.history,
                    dialog_state=self._dialog_state(ctx, products),
                    catalog=catalog.catalog_names(products),
                )
                return analysis, False
            except AIUnavailable as exc:
                log.warning("Claude unavailable (%s), using rule-based fallback", exc)
        return parse_message(text, products), True

    # ------------------------------------------------------------------ public API

    async def handle_text(self, user: UserInfo, text: str, ctx: DialogContext) -> BotReply:
        clean = mask_personal_data(text.strip()[:MAX_TEXT_LENGTH])
        products = await self._products()
        analysis, _degraded = await self._analyze(user, clean, ctx, products)
        reply = await self._dispatch(user, analysis, ctx, products)
        ctx.push(clean, reply.text, self._settings.history_max_messages)
        return reply

    async def handle_photo(
        self, user: UserInfo, image: bytes, media_type: str, caption: str | None, ctx: DialogContext
    ) -> BotReply:
        if not self._ai.enabled or not self._ai_limiter.allow(user.id):
            return BotReply(texts.PHOTO_AI_DOWN)
        products = await self._products()
        caption_clean = mask_personal_data((caption or "")[:300])
        try:
            analysis = await self._ai.analyze_photo(
                image=image, media_type=media_type, caption=caption_clean, catalog=catalog.catalog_names(products)
            )
        except AIUnavailable as exc:
            log.warning("Photo analysis unavailable: %s", exc)
            return BotReply(texts.PHOTO_AI_DOWN)

        if not analysis.is_sneaker:
            return BotReply(texts.PHOTO_NOT_SNEAKER)
        caption_size = self._valid_size(parse_message(caption_clean, products).size) if caption_clean else None
        size = caption_size or ctx.size_decimal()
        confidence = max(0.0, min(1.0, analysis.confidence))
        if confidence < PHOTO_CONFIDENCE_THRESHOLD or not analysis.model:
            hint = ""
            if analysis.brand:
                hint = f"Схоже, це кросівки {texts.h(analysis.brand)}, але модель визначити впевнено не вдалося."
            return BotReply(_join(hint, texts.PHOTO_UNSURE))

        recognised = texts.h(f"{analysis.brand or ''} {analysis.model}".strip())
        matches = catalog.match_products(products, brand=analysis.brand, model=analysis.model)
        if matches:
            product = matches[0]
            ctx.product_id, ctx.last_shown = product.id, [product.id]
            ctx.size = texts.fmt_size(size) if size else None
            intro = f"Схоже, на фото <b>{recognised}</b>. {texts.PHOTO_DISCLAIMER}"
            reply = self._product_answer(product, size, products, intro=intro)
        else:
            criteria = Criteria(
                brand=analysis.brand,
                color=analysis.colors[0] if analysis.colors else None,
                size=size,
            )
            alternatives = catalog.find_alternatives(products, reference=None, criteria=criteria)
            intro = f"Схоже, на фото <b>{recognised}</b>. {texts.NO_EXACT_MATCH}"
            reply = self._alternatives_reply(intro, alternatives, size, ctx, product=None, query=f"{analysis.brand} {analysis.model}")
        ctx.push("[фото кросівок]", reply.text, self._settings.history_max_messages)
        return reply

    async def show_product(self, product_id: int, ctx: DialogContext) -> BotReply:
        async with self._session_factory() as session:
            product = await catalog.get_product(session, product_id)
        if product is None or not product.is_active:
            return BotReply("Ця модель зараз недоступна. Напишіть, що шукаєте, — підберу інший варіант.")
        products = await self._products()
        ctx.product_id = product.id
        return self._product_answer(product, ctx.size_decimal(), products)

    async def choose_size(self, product_id: int, size_text: str, ctx: DialogContext) -> BotReply:
        size = self._valid_size(size_text)
        products = await self._products()
        product = next((p for p in products if p.id == product_id), None)
        if product is None:
            return BotReply("Ця модель зараз недоступна. Напишіть, що шукаєте, — підберу інший варіант.")
        ctx.product_id, ctx.size = product.id, texts.fmt_size(size) if size else None
        return self._product_answer(product, size, products)

    async def alternatives(self, product_id: int, size_text: str, ctx: DialogContext) -> BotReply:
        size = self._valid_size(size_text) if size_text else None
        products = await self._products()
        product = next((p for p in products if p.id == product_id), None)
        alternatives = catalog.find_alternatives(products, reference=product, criteria=Criteria(size=size))
        intro = "Ось схожі моделі, які зараз є в наявності" + (f" у розмірі {texts.fmt_size(size)}:" if size else ":")
        if not alternatives:
            intro = ""
        return self._alternatives_reply(intro, alternatives, size, ctx, product=product, query=None)

    async def checkout_check(self, product_id: int, size_text: str, ctx: DialogContext) -> BotReply:
        """Re-check stock right before the checkout starts."""
        size = self._valid_size(size_text)
        products = await self._products()
        product = next((p for p in products if p.id == product_id), None)
        if product is None or size is None:
            return BotReply("Ця модель зараз недоступна. Напишіть, що шукаєте, — підберу інший варіант.")
        ctx.product_id, ctx.size = product.id, texts.fmt_size(size)
        return self._start_order(product, size, products, ctx)

    async def my_orders(self, telegram_id: int) -> str:
        async with self._session_factory() as session:
            orders = (
                await session.scalars(
                    select(Order)
                    .options(selectinload(Order.items))
                    .where(Order.telegram_id == telegram_id)
                    .order_by(Order.id.desc())
                    .limit(10)
                )
            ).all()
        return texts.my_orders(orders)

    # ------------------------------------------------------------------ dialog logic

    async def _dispatch(
        self, user: UserInfo, a: MessageAnalysis, ctx: DialogContext, products: Sequence[Product]
    ) -> BotReply:
        phrase = _safe_phrase(a.reply)
        size = self._valid_size(a.size)
        by_id = {p.id: p for p in products}
        max_price = Decimal(a.max_price) if a.max_price and a.max_price > 0 else None
        purpose = a.purpose if a.purpose and a.purpose != "any" else None

        if a.intent == "off_topic":
            return BotReply(texts.OFF_TOPIC)
        if a.intent == "order_status":
            return BotReply(await self.my_orders(user.id))
        if a.intent == "cancel":
            ctx.product_id, ctx.size, ctx.last_shown = None, None, []
            return BotReply(phrase or "Добре, скасував вибір. Що підібрати для вас? 👟")

        # ---- resolve the product the client talks about
        product: Product | None = None
        matches: list[Product] = []
        if a.selected_option and 0 < a.selected_option <= len(ctx.last_shown):
            product = by_id.get(ctx.last_shown[a.selected_option - 1])
        if product is None and a.model:
            matches = catalog.match_products(products, brand=a.brand, model=a.model)
            if len(matches) == 1 or (matches and catalog.model_score(matches[0], a.model, a.brand) >= 95):
                product = matches[0]
        if product is None and not a.model and not a.brand and ctx.product_id in by_id:
            if a.intent in ("check_size", "start_order", "product_info", "alternatives") or size is not None:
                product = by_id[ctx.product_id]
        if size is None and product is not None and product.id == ctx.product_id:
            size = ctx.size_decimal()

        if product is not None:
            ctx.product_id = product.id
            ctx.size = texts.fmt_size(size) if size is not None else None
            if a.intent == "start_order":
                if size is None:
                    return self._product_answer(product, None, products, intro=phrase or "Чудовий вибір!")
                return self._start_order(product, size, products, ctx)
            if a.intent == "alternatives":
                alts = catalog.find_alternatives(products, reference=product, criteria=Criteria(size=size))
                intro = "Ось схожі моделі, які зараз є в наявності:" if alts else ""
                return self._alternatives_reply(_join(phrase, intro), alts, size, ctx, product=product, query=None)
            return self._product_answer(product, size, products, intro=phrase)

        if len(matches) > 1:
            ctx.last_shown = [p.id for p in matches]
            return BotReply(
                _join(phrase, "Знайшов кілька варіантів:", texts.product_list(matches), texts.CHOOSE_PROMPT),
                keyboard=kb.products_keyboard(matches),
            )

        criteria = Criteria(brand=a.brand, color=a.color, max_price=max_price, purpose=purpose, size=size)

        if a.model:
            # The client named a concrete model that is not in the catalog.
            alts = catalog.find_alternatives(products, reference=None, criteria=criteria)
            asked = texts.h(f"{a.brand or ''} {a.model}".strip())
            intro = f"Моделі <b>{asked}</b> у нашому каталозі зараз немає."
            return self._alternatives_reply(_join(phrase, intro), alts, size, ctx, product=None, query=f"{a.brand or ''} {a.model}")

        if a.brand:
            found = catalog.filter_products(products, criteria)
            if found:
                ctx.last_shown = [p.id for p in found]
                lead = phrase or f"Із задоволенням допоможу! Яку модель {texts.h(a.brand)} шукаєте?"
                return BotReply(
                    _join(lead, "Ось що є в наявності:", texts.product_list(found), texts.CHOOSE_PROMPT),
                    keyboard=kb.products_keyboard(found),
                )
            if catalog.brand_products(products, a.brand):
                intro = f"Моделей {texts.h(a.brand)} із такими параметрами зараз немає в наявності."
            else:
                intro = f"Кросівок {texts.h(a.brand)} у нашому каталозі зараз немає."
            alts = catalog.find_alternatives(
                products, reference=None, criteria=Criteria(color=a.color, max_price=max_price, purpose=purpose, size=size)
            )
            return self._alternatives_reply(_join(phrase, intro), alts, size, ctx, product=None, query=a.brand)

        if a.color or max_price or purpose or (size is not None and a.intent in ("search", "check_size")):
            found = catalog.filter_products(products, criteria)
            if found:
                ctx.last_shown = [p.id for p in found]
                return BotReply(
                    _join(phrase, "Ось що можу запропонувати з наявного:", texts.product_list(found), texts.CHOOSE_PROMPT),
                    keyboard=kb.products_keyboard(found),
                )
            return self._alternatives_reply(
                _join(phrase, "За цими параметрами зараз нічого немає в наявності."), [], size, ctx, product=None,
                query=texts.h(a.color or "") or None,
            )

        if a.intent in ("start_order", "check_size", "product_info"):
            return BotReply(_join(phrase, texts.ASK_WHICH_MODEL))

        if a.intent in ("greeting", "search", "alternatives", "choose_option"):
            top = catalog.popular(products)
            lead = phrase or (texts.GREETING if a.intent == "greeting" else texts.ASK_WHICH_MODEL)
            if top and a.intent != "greeting":
                ctx.last_shown = [p.id for p in top]
                return BotReply(_join(lead, "Популярні моделі в наявності:", texts.product_list(top)), keyboard=kb.products_keyboard(top))
            return BotReply(lead)

        return BotReply(phrase or texts.NOT_UNDERSTOOD)

    def _product_answer(
        self, product: Product, size: Decimal | None, products: Sequence[Product], *, intro: str = ""
    ) -> BotReply:
        photo = None
        if product.photos:
            first = product.photos[0]
            photo = first.telegram_file_id or first.url
        sizes_in_stock = texts.available_sizes(product)

        if size is None:
            if sizes_in_stock:
                return BotReply(
                    _join(intro, texts.product_card(product), texts.ASK_SIZE),
                    keyboard=kb.sizes_keyboard(product.id, sizes_in_stock),
                    photo=photo,
                )
            alts = catalog.find_alternatives(products, reference=product, criteria=Criteria())
            return self._alternatives_reply(
                _join(intro, texts.product_card(product)), alts, None, None, product=product, query=None, photo=photo
            )

        row = catalog.size_row(product, size)
        title = texts.h(product.title)
        if row is not None and row.available > 0:
            text = _join(
                intro,
                f"✅ <b>{title}</b> у розмірі <b>{texts.fmt_size(size)}</b> є в наявності.{texts.demo_mark(product)}\n"
                f"Ціна: <b>{texts.fmt_price(product.price)}</b>",
                "Оформити замовлення?",
            )
            return BotReply(text, keyboard=kb.order_offer_keyboard(product.id, size), photo=photo)

        other = f"Є розміри: {texts.fmt_sizes(sizes_in_stock)}." if sizes_in_stock else ""
        alts = catalog.find_alternatives(products, reference=product, criteria=Criteria(size=size))
        intro_text = _join(
            intro,
            f"На жаль, <b>{title}</b> у розмірі {texts.fmt_size(size)} зараз немає. {other}".strip(),
            "Але можу запропонувати схожі моделі:" if alts else "",
        )
        return self._alternatives_reply(intro_text, alts, size, None, product=product, query=None, photo=photo)

    def _alternatives_reply(
        self,
        intro: str,
        alternatives: Sequence[Product],
        size: Decimal | None,
        ctx: DialogContext | None,
        *,
        product: Product | None,
        query: str | None,
        photo: str | None = None,
    ) -> BotReply:
        if alternatives:
            if ctx is not None:
                ctx.last_shown = [p.id for p in alternatives]
            lines = [f"{i}. {texts.h(p.title)} — {texts.fmt_price(p.price)}{texts.demo_mark(p)}" for i, p in enumerate(alternatives, 1)]
            return BotReply(
                _join(intro, "\n".join(lines), texts.ALT_QUESTION),
                keyboard=kb.alternatives_keyboard(alternatives, size),
                photo=photo,
            )
        return BotReply(
            _join(intro, texts.NO_ALTERNATIVES),
            keyboard=kb.waitlist_keyboard(product.id if product else None, size),
            photo=photo,
        )

    def _start_order(self, product: Product, size: Decimal, products: Sequence[Product], ctx: DialogContext) -> BotReply:
        row = catalog.size_row(product, size)
        if row is None or row.available <= 0:
            return self._product_answer(product, size, products)
        return BotReply(text="", checkout=CheckoutRequest(product_id=product.id, size=size))
