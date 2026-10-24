"""Admin: catalog browsing, /add_product wizard, /edit_product."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from pydantic import ValidationError
from sqlalchemy import delete, func, select

from app import texts
from app.database.models import Product, ProductPhoto
from app.database.session import SessionFactory
from app.keyboards.admin import (
    AdminCategoryCb,
    AdminPageCb,
    AdminProductCb,
    categories_keyboard,
    product_edit_keyboard,
    products_page_keyboard,
)
from app.schemas.catalog import ALLOWED_CATEGORIES, ProductIn, parse_sizes_spec
from app.services import catalog, reports, waitlist
from app.services.catalog_import import set_sizes, upsert_product
from app.services.inventory import InvalidStockChange
from app.services.notifications import NotificationSender

log = logging.getLogger(__name__)
router = Router(name="admin_products")

PAGE_SIZE = 10
NOT_COMMAND = F.text & ~F.text.startswith("/")


class AddProduct(StatesGroup):
    brand = State()
    model = State()
    sku = State()
    color = State()
    price = State()
    category = State()
    description = State()
    sizes = State()
    photos = State()


class EditProduct(StatesGroup):
    value = State()
    photos = State()


def _parse_price(text: str) -> Decimal | None:
    try:
        value = Decimal(text.replace(" ", "").replace(",", ".").replace("грн", ""))
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if Decimal("0") < value < Decimal("10000000") else None


# ------------------------------------------------------------------ catalog list


async def send_products_page(message: Message, session_factory: SessionFactory, *, page: int, query: str) -> None:
    async with session_factory() as session:
        products = await catalog.search_admin(session, query, offset=page * PAGE_SIZE, limit=PAGE_SIZE + 1)
        total = await session.scalar(select(func.count(Product.id)))
    has_next = len(products) > PAGE_SIZE
    products = products[:PAGE_SIZE]
    if not products:
        await message.answer("Нічого не знайдено." if query else "Каталог порожній. Додайте товар: /add_product")
        return
    header = f"<b>📦 Каталог</b> (усього {total}{', пошук: ' + texts.h(query) if query else ''}, сторінка {page + 1})"
    lines = [header] + [
        f"{'' if p.is_active else '🙈 '}<code>{texts.h(p.sku)}</code> {texts.h(p.title)} — {texts.fmt_price(p.price)}"
        for p in products
    ]
    await message.answer("\n".join(lines), reply_markup=products_page_keyboard(products, page, has_next, query))


@router.message(Command("products"))
async def cmd_products(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    query = re.sub(r"[^\w\s.-]", "", (command.args or "").strip())[:30]
    await send_products_page(message, session_factory, page=0, query=query)


@router.callback_query(AdminPageCb.filter(F.kind == "products"))
async def cb_products_page(callback: CallbackQuery, callback_data: AdminPageCb, session_factory: SessionFactory) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await send_products_page(callback.message, session_factory, page=max(0, callback_data.page), query=callback_data.query)


# ------------------------------------------------------------------ add product wizard


async def begin_add_product(message: Message, state: FSMContext) -> None:
    await state.set_state(AddProduct.brand)
    await state.update_data(new_product={})
    await message.answer("➕ <b>Новий товар</b>\nКрок 1/9. Вкажіть бренд (наприклад, Nike). /cancel — скасувати.")


@router.message(Command("add_product"))
async def cmd_add_product(message: Message, state: FSMContext) -> None:
    await begin_add_product(message, state)


async def _store(state: FSMContext, **values: object) -> dict:
    data = await state.get_data()
    product = dict(data.get("new_product") or {})
    product.update(values)
    await state.update_data(new_product=product)
    return product


@router.message(AddProduct.brand, NOT_COMMAND)
async def add_brand(message: Message, state: FSMContext) -> None:
    brand = (message.text or "").strip()
    if not 1 <= len(brand) <= 64:
        await message.answer("Бренд має містити 1–64 символи.")
        return
    await _store(state, brand=brand)
    await state.set_state(AddProduct.model)
    await message.answer("Крок 2/9. Назва моделі (без бренду), наприклад: Vomero 5")


@router.message(AddProduct.model, NOT_COMMAND)
async def add_model(message: Message, state: FSMContext) -> None:
    model = (message.text or "").strip()
    if not 1 <= len(model) <= 128:
        await message.answer("Назва має містити 1–128 символів.")
        return
    await _store(state, model=model)
    await state.set_state(AddProduct.sku)
    await message.answer("Крок 3/9. Артикул (латиниця, цифри, «-», «_»), наприклад: NV5-GREY")


@router.message(AddProduct.sku, NOT_COMMAND)
async def add_sku(message: Message, state: FSMContext, session_factory: SessionFactory) -> None:
    sku = (message.text or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,63}", sku):
        await message.answer("Некоректний артикул. Дозволено латиницю, цифри, «.», «_», «-».")
        return
    async with session_factory() as session:
        if await catalog.get_product_by_sku(session, sku):
            await message.answer("Товар з таким артикулом уже існує. Вкажіть інший або змініть існуючий: /edit_product " + sku)
            return
    await _store(state, sku=sku)
    await state.set_state(AddProduct.color)
    await message.answer("Крок 4/9. Колір (наприклад: сірий, срібний)")


@router.message(AddProduct.color, NOT_COMMAND)
async def add_color(message: Message, state: FSMContext) -> None:
    await _store(state, color=(message.text or "").strip()[:128])
    await state.set_state(AddProduct.price)
    await message.answer("Крок 5/9. Ціна в гривнях (наприклад: 5800)")


@router.message(AddProduct.price, NOT_COMMAND)
async def add_price(message: Message, state: FSMContext) -> None:
    price = _parse_price(message.text or "")
    if price is None:
        await message.answer("Вкажіть ціну числом більше нуля, наприклад 5800.")
        return
    await _store(state, price=str(price))
    await state.set_state(AddProduct.category)
    await message.answer("Крок 6/9. Оберіть категорію:", reply_markup=categories_keyboard(ALLOWED_CATEGORIES))


@router.callback_query(AddProduct.category, AdminCategoryCb.filter())
async def add_category(callback: CallbackQuery, callback_data: AdminCategoryCb, state: FSMContext) -> None:
    await callback.answer()
    if callback_data.category not in ALLOWED_CATEGORIES:
        return
    await _store(state, category=callback_data.category)
    await state.set_state(AddProduct.description)
    if isinstance(callback.message, Message):
        await callback.message.answer("Крок 7/9. Короткий опис (або «-», щоб пропустити)")


@router.message(AddProduct.description, NOT_COMMAND)
async def add_description(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    await _store(state, description="" if text == "-" else text[:4000])
    await state.set_state(AddProduct.sizes)
    await message.answer("Крок 8/9. Розміри та кількість пар у форматі: 42:3, 43:1, 44:0")


@router.message(AddProduct.sizes, NOT_COMMAND)
async def add_sizes(message: Message, state: FSMContext) -> None:
    try:
        sizes = parse_sizes_spec(message.text or "")
    except (ValueError, ValidationError) as exc:
        await message.answer(f"❗ {texts.h(_error_text(exc))}")
        return
    await _store(state, sizes=[{"size": str(s.size), "quantity": s.quantity} for s in sizes], photos=[])
    await state.set_state(AddProduct.photos)
    await message.answer("Крок 9/9. Надішліть одне або кілька фото товару, потім /done. Щоб без фото — одразу /done.")


@router.message(AddProduct.photos, F.photo)
async def add_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    photos = list((data.get("new_product") or {}).get("photos") or [])
    if len(photos) >= 10:
        await message.answer("Максимум 10 фото. Надішліть /done.")
        return
    photos.append(message.photo[-1].file_id)
    await _store(state, photos=photos)
    await message.answer(f"Фото додано ({len(photos)}). Ще фото або /done.")


@router.message(AddProduct.photos, Command("done"))
async def add_done(message: Message, state: FSMContext, session_factory: SessionFactory) -> None:
    data = (await state.get_data()).get("new_product") or {}
    try:
        product_in = ProductIn.model_validate({**data, "photo_urls": []})
    except ValidationError as exc:
        await message.answer(f"❗ Не вдалося зберегти: {texts.h(_error_text(exc))}\nПочніть заново: /add_product")
        await state.set_state(None)
        return
    async with session_factory() as session, session.begin():
        product, _ = await upsert_product(session, product_in, actor=message.from_user.id)
        for position, file_id in enumerate(data.get("photos") or []):
            session.add(ProductPhoto(product_id=product.id, telegram_file_id=file_id, position=position))
    await state.set_state(None)
    await state.update_data(new_product=None)
    await message.answer(f"✅ Товар <b>{texts.h(product.title)}</b> ({texts.h(product.sku)}) додано.")
    await _send_card(message, session_factory, product.id)


# ------------------------------------------------------------------ edit product


async def _send_card(message: Message, session_factory: SessionFactory, product_id: int) -> None:
    async with session_factory() as session:
        product = await catalog.get_product(session, product_id)
        if product is None:
            await message.answer("Товар не знайдено.")
            return
        text = await reports.stock_text(session, product)
    details = (
        f"\nКолір: {texts.h(product.color) or '—'} · Категорія: {texts.h(product.category)}"
        f"\nФото: {len(product.photos)}\nОпис: {texts.h(product.description) or '—'}"
    )
    await message.answer(text + details, reply_markup=product_edit_keyboard(product))


@router.message(Command("edit_product"))
async def cmd_edit_product(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    arg = (command.args or "").strip()
    if not arg:
        await message.answer("Використання: /edit_product АРТИКУЛ (список артикулів: /products)")
        return
    async with session_factory() as session:
        product = await catalog.get_product_by_sku(session, arg)
        if product is None and arg.isdigit():
            product = await catalog.get_product(session, int(arg))
    if product is None:
        await message.answer("Товар не знайдено.")
        return
    await _send_card(message, session_factory, product.id)


PROMPTS = {
    "price": "Нова ціна в гривнях (наприклад: 5900):",
    "color": "Новий колір:",
    "name": "Нові бренд і модель через «|», наприклад: Nike | Vomero 5",
    "description": "Новий опис (або «-», щоб очистити):",
    "sizes": "Розміри та кількість у форматі 42:3, 44:0 (вказані розміри буде встановлено, інші не зміняться):",
}


@router.callback_query(AdminProductCb.filter())
async def cb_product(
    callback: CallbackQuery, callback_data: AdminProductCb, state: FSMContext, session_factory: SessionFactory
) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    action, product_id = callback_data.action, callback_data.product_id
    if action == "view":
        await _send_card(message, session_factory, product_id)
    elif action in PROMPTS:
        await state.set_state(EditProduct.value)
        await state.update_data(edit={"product_id": product_id, "field": action})
        await message.answer(PROMPTS[action] + "\n/cancel — скасувати")
    elif action == "photo":
        await state.set_state(EditProduct.photos)
        await state.update_data(edit={"product_id": product_id, "field": "photo"})
        await message.answer("Надішліть фото (можна кілька), потім /done.")
    elif action == "clear_photos":
        async with session_factory() as session, session.begin():
            await session.execute(delete(ProductPhoto).where(ProductPhoto.product_id == product_id))
        await message.answer("Фото видалено.")
        await _send_card(message, session_factory, product_id)
    elif action == "toggle":
        async with session_factory() as session, session.begin():
            product = await session.get(Product, product_id)
            if product is not None:
                product.is_active = not product.is_active
        await _send_card(message, session_factory, product_id)


@router.message(EditProduct.value, NOT_COMMAND)
async def edit_value(
    message: Message, state: FSMContext, session_factory: SessionFactory, sender: NotificationSender
) -> None:
    edit = (await state.get_data()).get("edit") or {}
    field, product_id = edit.get("field"), edit.get("product_id")
    text = (message.text or "").strip()
    notified = 0
    try:
        async with session_factory() as session, session.begin():
            product = await catalog.get_product(session, int(product_id or 0))
            if product is None:
                await message.answer("Товар не знайдено.")
                await state.set_state(None)
                return
            if field == "price":
                price = _parse_price(text)
                if price is None:
                    await message.answer("Вкажіть ціну числом більше нуля.")
                    return
                product.price = price
            elif field == "color":
                product.color = text[:128]
            elif field == "description":
                product.description = "" if text == "-" else text[:4000]
            elif field == "name":
                parts = [p.strip() for p in text.split("|")]
                if len(parts) != 2 or not all(parts) or len(parts[0]) > 64 or len(parts[1]) > 128:
                    await message.answer("Формат: Бренд | Модель")
                    return
                product.brand, product.model = parts
            elif field == "sizes":
                changes = await set_sizes(
                    session, product, parse_sizes_spec(text), actor=message.from_user.id, comment="admin edit"
                )
                await session.refresh(product, ["sizes"])
                for row, previous in changes:
                    current = catalog.size_row(product, row.size)
                    if current is not None and previous <= 0 < current.available:
                        notified += await waitlist.notify_restock(session, product, row.size)
    except (ValueError, ValidationError, InvalidStockChange) as exc:
        await message.answer(f"❗ {texts.h(_error_text(exc))}")
        return
    await state.set_state(None)
    await message.answer("✅ Збережено." + (f" Сповіщено клієнтів: {notified}." if notified else ""))
    if notified:
        await sender.deliver_due()
    await _send_card(message, session_factory, int(product_id))


@router.message(EditProduct.photos, F.photo)
async def edit_photo(message: Message, state: FSMContext, session_factory: SessionFactory) -> None:
    edit = (await state.get_data()).get("edit") or {}
    product_id = int(edit.get("product_id") or 0)
    async with session_factory() as session, session.begin():
        count = await session.scalar(select(func.count(ProductPhoto.id)).where(ProductPhoto.product_id == product_id))
        if (count or 0) >= 10:
            await message.answer("Максимум 10 фото на товар. /done")
            return
        session.add(ProductPhoto(product_id=product_id, telegram_file_id=message.photo[-1].file_id, position=count or 0))
    await message.answer("Фото додано. Ще фото або /done.")


@router.message(EditProduct.photos, Command("done"))
async def edit_photos_done(message: Message, state: FSMContext, session_factory: SessionFactory) -> None:
    edit = (await state.get_data()).get("edit") or {}
    await state.set_state(None)
    await _send_card(message, session_factory, int(edit.get("product_id") or 0))


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(err.get("msg", "") for err in exc.errors())
    return str(exc)
