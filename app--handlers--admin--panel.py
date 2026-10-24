"""Admin: /admin panel, /help, /stats, /stock, /set_stock, /export_orders, /cancel."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardRemove

from app import texts
from app.database.session import SessionFactory
from app.handlers.admin.orders import orders_list_text
from app.handlers.admin.products import begin_add_product, send_products_page
from app.keyboards.admin import AdminMenuCb, admin_menu_keyboard, orders_list_keyboard
from app.schemas.catalog import parse_size
from app.services import catalog, inventory, reports, waitlist
from app.services.inventory import InvalidStockChange, utcnow
from app.services.notifications import NotificationSender

router = Router(name="admin_panel")


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await message.answer("<b>Панель адміністратора</b>", reply_markup=admin_menu_keyboard())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.ADMIN_HELP + "\n\n" + texts.HELP_CLIENT)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await message.answer("Дію скасовано.", reply_markup=ReplyKeyboardRemove())


@router.message(Command("stats"))
async def cmd_stats(message: Message, session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        await message.answer(await reports.stats_text(session))


@router.message(Command("stock"))
async def cmd_stock(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    async with session_factory() as session:
        product = None
        if command.args:
            product = await catalog.get_product_by_sku(session, command.args)
            if product is None:
                await message.answer("Товар з таким артикулом не знайдено.")
                return
        text = await reports.stock_text(session, product)
    for chunk in _chunks(text):
        await message.answer(chunk)


@router.message(Command("set_stock"))
async def cmd_set_stock(
    message: Message, command: CommandObject, session_factory: SessionFactory, sender: NotificationSender
) -> None:
    parts = (command.args or "").split()
    if len(parts) != 3 or not parts[2].isdigit():
        await message.answer("Використання: /set_stock АРТИКУЛ РОЗМІР КІЛЬКІСТЬ\nНаприклад: /set_stock NV5 44 2")
        return
    try:
        size = parse_size(parts[1])
    except ValueError as exc:
        await message.answer(str(exc))
        return
    try:
        async with session_factory() as session, session.begin():
            product = await catalog.get_product_by_sku(session, parts[0])
            if product is None:
                await message.answer("Товар з таким артикулом не знайдено.")
                return
            row, previous = await inventory.set_quantity(
                session, product_id=product.id, size=size, quantity=int(parts[2]), actor=message.from_user.id, comment="/set_stock"
            )
            notified = 0
            if previous <= 0 < row.available:
                notified = await waitlist.notify_restock(session, product, size)
    except InvalidStockChange as exc:
        await message.answer(f"❗ {texts.h(exc)}")
        return
    extra = f"\nСповіщено клієнтів зі списку очікування: {notified}" if notified else ""
    await message.answer(
        f"✅ {texts.h(product.title)}, р. {texts.fmt_size(size)}: на складі {row.quantity}, "
        f"у резерві {row.reserved}, доступно {row.available}{extra}"
    )
    if notified:
        await sender.deliver_due()


@router.message(Command("export_orders"))
async def cmd_export(message: Message, command: CommandObject, session_factory: SessionFactory) -> None:
    days = int(command.args) if command.args and command.args.strip().isdigit() else None
    await _send_export(message, session_factory, days)


async def _send_export(message: Message, session_factory: SessionFactory, days: int | None) -> None:
    async with session_factory() as session:
        data = await reports.export_orders_csv(session, days)
    filename = f"orders_{utcnow():%Y%m%d_%H%M}.csv"
    await message.answer_document(
        BufferedInputFile(data, filename=filename),
        caption="Експорт замовлень (містить персональні дані — не пересилайте стороннім).",
    )


@router.callback_query(AdminMenuCb.filter())
async def cb_menu(
    callback: CallbackQuery, callback_data: AdminMenuCb, state: FSMContext, session_factory: SessionFactory
) -> None:
    await callback.answer()
    message = callback.message
    if not isinstance(message, Message):
        return
    action = callback_data.action
    if action == "stats":
        async with session_factory() as session:
            await message.answer(await reports.stats_text(session))
    elif action == "stock":
        async with session_factory() as session:
            text = await reports.stock_text(session)
        for chunk in _chunks(text):
            await message.answer(chunk)
    elif action in ("orders", "orders_pending"):
        text, orders = await orders_list_text(session_factory, "pending" if action == "orders_pending" else None)
        await message.answer(text, reply_markup=orders_list_keyboard(orders) if orders else None)
    elif action == "export":
        await _send_export(message, session_factory, None)
    elif action == "products":
        await send_products_page(message, session_factory, page=0, query="")
    elif action == "add_product":
        await begin_add_product(message, state)
    elif action == "help":
        await message.answer(texts.ADMIN_HELP)


def _chunks(text: str, limit: int = 3800) -> list[str]:
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current.strip():
        chunks.append(current)
    return chunks
