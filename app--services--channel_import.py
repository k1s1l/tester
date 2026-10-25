"""Catalog import from a Telegram channel.

Two sources produce the same result (a product draft for the admin):
  * new posts in the shop channel (the bot must be an administrator of the channel);
  * old posts forwarded to the bot by an admin (bots cannot read channel history themselves).

Claude only reads the post. The admin confirms every draft (unless CHANNEL_AUTO_PUBLISH=true),
and stock is written through the usual inventory functions with the stock journal.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app import texts
from app.config import Settings
from app.database.models import DraftStatus, Product, ProductDraft, ProductPhoto
from app.database.session import SessionFactory
from app.keyboards.admin import draft_keyboard
from app.schemas.ai import ParsedProduct
from app.schemas.catalog import ALLOWED_CATEGORIES, ProductIn, SizeStockIn, parse_size
from app.services import catalog, inventory, waitlist
from app.services.ai import AIUnavailable, ClaudeService
from app.services.catalog_import import upsert_product
from app.services.images import ImageRejected, validate_image

log = logging.getLogger(__name__)

MAX_PRODUCTS_PER_POST = 5
MAX_PHOTOS = 10
AUTO_PUBLISH_MIN_CONFIDENCE = 0.75
CAPTION_LIMIT = 1024


class DraftError(Exception):
    pass


class DraftAlreadyHandled(DraftError):
    def __init__(self, status: str, product_id: int | None) -> None:
        super().__init__(status)
        self.status = status
        self.product_id = product_id


class DraftInvalid(DraftError):
    pass


@dataclass
class IncomingPost:
    source_chat_id: int
    source_message_id: int
    text: str
    photo_file_ids: list[str]
    reply_chat_ids: list[int]
    from_forward: bool


@dataclass
class SaveResult:
    product: Product
    created: bool
    restock_notified: int = 0


# ------------------------------------------------------------------ payload helpers


def suggest_sku(brand: str, model: str) -> str:
    base = re.sub(r"[^A-Z0-9]+", "-", f"{brand}-{model}".upper()).strip("-")
    return (base or "ITEM")[:48]


def build_payload(parsed: ParsedProduct, default_quantity: int) -> dict[str, Any]:
    sizes: dict[str, dict[str, Any]] = {}
    for item in parsed.sizes:
        try:
            size = parse_size(item.size)
        except ValueError:
            continue
        stated = item.quantity is not None and 0 <= item.quantity <= 1000
        sizes[str(size)] = {
            "size": str(size),
            "quantity": item.quantity if stated else default_quantity,
            "guessed": not stated,
        }
    category = parsed.category if parsed.category in ALLOWED_CATEGORIES else "lifestyle"
    price = parsed.price_uah if parsed.price_uah and parsed.price_uah > 0 else None
    return {
        "brand": parsed.brand.strip()[:64],
        "model": parsed.model.strip()[:128],
        "color": parsed.color.strip()[:128],
        "category": category,
        "price": str(price) if price else None,
        "description": parsed.description.strip()[:1000],
        "sizes": sorted(sizes.values(), key=lambda s: Decimal(s["size"])),
        "sku": suggest_sku(parsed.brand, parsed.model),
    }


def payload_warnings(payload: dict[str, Any]) -> list[str]:
    warnings = []
    if not payload.get("price"):
        warnings.append("⚠️ Ціну в пості не знайдено — натисніть «💰 Ціна».")
    if not payload.get("sizes"):
        warnings.append("⚠️ Розміри не знайдено — натисніть «📏 Розміри».")
    elif any(s.get("guessed") for s in payload["sizes"]):
        warnings.append("ℹ️ Кількість пар у пості не вказана — поставив за замовчуванням. Змінити: «📏 Розміри».")
    return warnings


def draft_text(payload: dict[str, Any], photos: int, existing: Product | None, header: str) -> str:
    sizes = ", ".join(f"{texts.fmt_size(Decimal(s['size']))} ({s['quantity']} шт.)" for s in payload["sizes"]) or "—"
    price = texts.fmt_price(Decimal(payload["price"])) if payload.get("price") else "не вказана"
    lines = [
        header,
        f"<b>{texts.h(payload['brand'])} {texts.h(payload['model'])}</b> — {price}",
        f"Колір: {texts.h(payload['color']) or '—'}",
        f"Категорія: {texts.CATEGORY_LABELS.get(payload['category'], payload['category'])}",
        f"Розміри (кількість): {sizes}",
        f"Фото: {photos}",
        f"Артикул: <code>{texts.h(payload['sku'])}</code>",
    ]
    if payload.get("description"):
        lines.append(f"Опис: {texts.h(payload['description'])}")
    if existing is not None:
        lines.append(
            f"\n🔁 Схожий товар уже є в каталозі: <code>{texts.h(existing.sku)}</code> {texts.h(existing.title)} "
            f"({texts.fmt_price(existing.price)}). Можна оновити його ціну й розміри або додати як новий."
        )
    lines += payload_warnings(payload)
    return "\n".join(lines)


def find_existing(products: list[Product], parsed: ParsedProduct) -> Product | None:
    for product in catalog.match_products(products, brand=parsed.brand, model=parsed.model):
        if not catalog.brand_matches(product, parsed.brand):
            continue
        if catalog.model_score(product, parsed.model, parsed.brand) < 90:
            continue
        if parsed.color and product.color and not catalog.color_matches(product, parsed.color):
            continue  # another colorway is a separate product
        return product
    return None


async def _real_products(session) -> list[Product]:
    result = await session.scalars(
        select(Product).options(selectinload(Product.sizes), selectinload(Product.photos)).where(Product.is_demo.is_(False))
    )
    return list(result.all())


async def _unique_sku(session, wanted: str) -> str:
    sku, n = wanted, 2
    while await session.scalar(select(Product.id).where(Product.sku == sku)):
        sku = f"{wanted[:44]}-{n}"
        n += 1
    return sku


# ------------------------------------------------------------------ saving


async def save_draft(
    session_factory: SessionFactory, settings: Settings, *, draft_id: int, mode: str, actor: int | None
) -> SaveResult:
    """mode="new" creates a product, mode="update" refreshes price/sizes/photos of the matched product."""
    async with session_factory() as session, session.begin():
        draft = await session.scalar(select(ProductDraft).where(ProductDraft.id == draft_id).with_for_update())
        if draft is None:
            raise DraftInvalid("Чернетку не знайдено")
        if draft.status != DraftStatus.PENDING:
            raise DraftAlreadyHandled(draft.status, draft.product_id)
        payload = draft.payload
        if not payload.get("price"):
            raise DraftInvalid("Спочатку вкажіть ціну (кнопка «💰 Ціна»)")
        sizes = [SizeStockIn(size=s["size"], quantity=int(s["quantity"])) for s in payload.get("sizes", [])]
        guessed = {Decimal(s["size"]).quantize(Decimal("0.1")) for s in payload.get("sizes", []) if s.get("guessed")}
        notified = 0

        if mode == "update":
            if draft.existing_product_id is None:
                raise DraftInvalid("Немає товару для оновлення")
            product = await catalog.get_product(session, draft.existing_product_id)
            if product is None:
                raise DraftInvalid("Товар для оновлення вже видалено")
            product.price = Decimal(payload["price"])
            product.is_active = True
            for row in sizes:
                current = catalog.size_row(product, row.size)
                quantity = row.quantity
                if current is not None:
                    if row.size in guessed:
                        # the post only says "available": keep the known quantity, ensure at least the default
                        quantity = max(current.quantity, quantity)
                    quantity = max(quantity, current.reserved)  # never below reserved pairs
                size_row, previous = await inventory.set_quantity(
                    session, product_id=product.id, size=row.size, quantity=quantity, actor=actor, comment="channel post"
                )
                if previous <= 0 < size_row.available:
                    notified += await waitlist.notify_restock(session, product, row.size)
            created = False
        else:
            data = ProductIn(
                brand=payload["brand"],
                model=payload["model"],
                sku=await _unique_sku(session, payload["sku"]),
                color=payload.get("color") or "",
                category=payload.get("category") or "lifestyle",
                price=Decimal(payload["price"]),
                description=payload.get("description") or "",
                sizes=sizes,
                is_active=True,
                is_demo=False,
            )
            product, _ = await upsert_product(session, data, actor=actor)
            created = True

        have_photos = await session.scalar(select(ProductPhoto.id).where(ProductPhoto.product_id == product.id).limit(1))
        if not have_photos:
            for position, file_id in enumerate(draft.photo_file_ids[:MAX_PHOTOS]):
                session.add(ProductPhoto(product_id=product.id, telegram_file_id=file_id, position=position))
        draft.status = DraftStatus.SAVED
        draft.product_id = product.id
        await session.flush()
        product_id = product.id

    async with session_factory() as session:
        product = await catalog.get_product(session, product_id)
    assert product is not None
    log.info("Draft %s saved as product %s (%s)", draft_id, product.sku, "created" if created else "updated")
    return SaveResult(product=product, created=created, restock_notified=notified)


async def skip_draft(session_factory: SessionFactory, draft_id: int) -> None:
    async with session_factory() as session, session.begin():
        draft = await session.scalar(select(ProductDraft).where(ProductDraft.id == draft_id).with_for_update())
        if draft is None:
            raise DraftInvalid("Чернетку не знайдено")
        if draft.status != DraftStatus.PENDING:
            raise DraftAlreadyHandled(draft.status, draft.product_id)
        draft.status = DraftStatus.SKIPPED


async def update_draft(session_factory: SessionFactory, draft_id: int, **changes: Any) -> ProductDraft:
    async with session_factory() as session, session.begin():
        draft = await session.scalar(select(ProductDraft).where(ProductDraft.id == draft_id).with_for_update())
        if draft is None:
            raise DraftInvalid("Чернетку не знайдено")
        if draft.status != DraftStatus.PENDING:
            raise DraftAlreadyHandled(draft.status, draft.product_id)
        draft.payload = {**draft.payload, **changes}
        await session.flush()
        return draft


# ------------------------------------------------------------------ importer


@dataclass
class _Album:
    post: IncomingPost
    timer: asyncio.Task | None = None
    parts: int = 0


class ChannelImporter:
    def __init__(
        self,
        bot: Any,
        session_factory: SessionFactory,
        settings: Settings,
        ai: ClaudeService,
        *,
        album_delay: float = 2.5,
        concurrency: int = 2,
    ) -> None:
        self._bot = bot
        self._session_factory = session_factory
        self._settings = settings
        self._ai = ai
        self.album_delay = album_delay
        self._albums: dict[str, _Album] = {}
        self._tasks: set[asyncio.Task] = set()
        self._semaphore = asyncio.Semaphore(concurrency)

    # --- intake -------------------------------------------------------------

    async def add_part(self, post: IncomingPost, *, album_key: str | None) -> None:
        """Single posts are processed at once; album parts are merged first (Telegram sends them separately)."""
        if album_key is None:
            self._spawn(self._process(post))
            return
        album = self._albums.get(album_key)
        if album is None:
            album = self._albums[album_key] = _Album(post=post)
        else:
            for file_id in post.photo_file_ids:
                if file_id not in album.post.photo_file_ids:
                    album.post.photo_file_ids.append(file_id)
            if not album.post.text and post.text:
                album.post.text = post.text
            album.post.source_message_id = min(album.post.source_message_id, post.source_message_id)
        album.parts += 1
        if album.timer is not None:
            album.timer.cancel()
        album.timer = self._spawn(self._flush_album(album_key))

    async def _flush_album(self, album_key: str) -> None:
        await asyncio.sleep(self.album_delay)
        album = self._albums.pop(album_key, None)
        if album is not None:
            await self._process(album.post)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def wait_idle(self) -> None:
        """Wait until all queued posts are processed (used by tests and on shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # --- processing ---------------------------------------------------------

    async def _process(self, post: IncomingPost) -> None:
        async with self._semaphore:
            try:
                await self._process_post(post)
            except Exception:
                log.exception("Failed to import channel post %s:%s", post.source_chat_id, post.source_message_id)
                await self._notify(post.reply_chat_ids, "❗ Не вдалося обробити пост із каналу. Спробуйте переслати його ще раз.")

    async def _process_post(self, post: IncomingPost) -> None:
        base_key = f"post:{post.source_chat_id}:{post.source_message_id}"
        async with self._session_factory() as session:
            seen = await session.scalar(
                select(ProductDraft.id).where(ProductDraft.source_key.like(f"{base_key}:%")).limit(1)
            )
            products = await _real_products(session)
            catalog_names = catalog.catalog_names(products)
        if seen:
            if post.from_forward:
                await self._notify(post.reply_chat_ids, "ℹ️ Цей пост уже оброблено раніше.")
            return
        if not post.text.strip() and not post.photo_file_ids:
            return
        if not self._ai.enabled:
            await self._notify(
                post.reply_chat_ids,
                "❗ Щоб розпізнавати пости, додайте ANTHROPIC_API_KEY. Поки що товар можна додати вручну: /add_product",
            )
            return

        image, media_type = await self._download_first_photo(post.photo_file_ids)
        try:
            analysis = await self._ai.parse_catalog_post(
                text=post.text, image=image, media_type=media_type, catalog=catalog_names
            )
        except AIUnavailable as exc:
            log.warning("Channel post analysis unavailable: %s", exc)
            await self._notify(post.reply_chat_ids, "❗ Claude зараз недоступний. Перешліть цей пост трохи пізніше.")
            return

        if not analysis.is_product_post or not analysis.products:
            if post.from_forward:
                await self._notify(post.reply_chat_ids, "ℹ️ Це не схоже на пост із кросівками на продаж — пропускаю.")
            return

        for index, parsed in enumerate(analysis.products[:MAX_PRODUCTS_PER_POST]):
            payload = build_payload(parsed, self._settings.channel_default_quantity)
            existing = find_existing(products, parsed)
            try:
                async with self._session_factory() as session, session.begin():
                    draft = ProductDraft(
                        source_key=f"{base_key}:{index}",
                        payload=payload,
                        photo_file_ids=post.photo_file_ids[:MAX_PHOTOS],
                        existing_product_id=existing.id if existing else None,
                        status=DraftStatus.PENDING,
                    )
                    session.add(draft)
                    await session.flush()
                    draft_id = draft.id
            except IntegrityError:
                continue  # the same post is being processed concurrently

            auto = (
                self._settings.channel_auto_publish
                and payload["price"]
                and payload["sizes"]
                and analysis.confidence >= AUTO_PUBLISH_MIN_CONFIDENCE
            )
            if auto:
                try:
                    result = await save_draft(
                        self._session_factory,
                        self._settings,
                        draft_id=draft_id,
                        mode="update" if existing else "new",
                        actor=None,
                    )
                except DraftError as exc:
                    log.warning("Auto-publish of draft %s failed: %s", draft_id, exc)
                else:
                    action = "оновлено" if not result.created else "додано"
                    await self._notify(
                        post.reply_chat_ids,
                        f"✅ Товар із каналу автоматично {action}: <code>{texts.h(result.product.sku)}</code>\n\n"
                        f"{texts.product_card(result.product)}\n\nЗмінити: /edit_product {texts.h(result.product.sku)}",
                    )
                    continue
            await self.send_draft_card(draft_id, post.reply_chat_ids)

    async def _download_first_photo(self, file_ids: list[str]) -> tuple[bytes | None, str | None]:
        if not file_ids:
            return None, None
        try:
            buffer = io.BytesIO()
            await self._bot.download(file_ids[0], destination=buffer)
            data = buffer.getvalue()
            return data, validate_image(data, int(self._settings.max_photo_mb * 1024 * 1024))
        except (TimeoutError, ImageRejected, TelegramAPIError, OSError, AttributeError) as exc:
            # the photo only helps recognition: the post is still analysed by its text
            log.info("Post photo not sent to Claude: %s", type(exc).__name__)
            return None, None

    async def draft_view(self, draft_id: int) -> tuple[str, InlineKeyboardMarkup | None, list[str]]:
        async with self._session_factory() as session:
            draft = await session.get(ProductDraft, draft_id)
            if draft is None:
                raise DraftInvalid("Чернетку не знайдено")
            existing = await catalog.get_product(session, draft.existing_product_id) if draft.existing_product_id else None
        text = draft_text(draft.payload, len(draft.photo_file_ids), existing, "🆕 <b>Товар із каналу</b> (чернетка)")
        keyboard = draft_keyboard(draft.id, existing_sku=existing.sku if existing else None)
        return text, keyboard, list(draft.photo_file_ids)

    async def send_draft_card(self, draft_id: int, chat_ids: list[int]) -> None:
        text, keyboard, photos = await self.draft_view(draft_id)
        for chat_id in chat_ids:
            try:
                if photos and len(text) <= CAPTION_LIMIT:
                    await self._bot.send_photo(chat_id=chat_id, photo=photos[0], caption=text, reply_markup=keyboard)
                else:
                    await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=keyboard)
            except TelegramAPIError as exc:
                log.error("Could not send product draft %s to %s: %s", draft_id, chat_id, type(exc).__name__)

    async def _notify(self, chat_ids: list[int], text: str) -> None:
        for chat_id in chat_ids:
            try:
                await self._bot.send_message(chat_id=chat_id, text=text)
            except TelegramAPIError as exc:
                log.error("Could not notify %s: %s", chat_id, type(exc).__name__)
