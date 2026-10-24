"""All client/admin facing texts (Ukrainian) and formatting helpers.

Facts (prices, sizes, stock) are always rendered here from database values,
never taken from the AI model output.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from decimal import Decimal
from html import escape

from app.database.models import Order, OrderStatus, Product

STATUS_LABELS: dict[str, str] = {
    OrderStatus.PENDING: "Очікує підтвердження",
    OrderStatus.CONFIRMED: "Підтверджено",
    OrderStatus.PAID: "Оплачено",
    OrderStatus.SHIPPED: "Відправлено",
    OrderStatus.COMPLETED: "Завершено",
    OrderStatus.REJECTED: "Відхилено",
    OrderStatus.CANCELLED: "Скасовано",
    OrderStatus.EXPIRED: "Резерв минув",
}

CATEGORY_LABELS = {
    "lifestyle": "повсякденні",
    "running": "бігові",
    "basketball": "баскетбольні",
    "training": "для тренувань",
    "walking": "для прогулянок",
}


def h(value: object) -> str:
    """HTML-escape any dynamic value (parse_mode=HTML)."""
    return escape(str(value), quote=False)


def fmt_price(value: Decimal | int | float) -> str:
    amount = Decimal(str(value))
    text = f"{amount:.2f}".rstrip("0").rstrip(".")
    return f"{text} грн"


def fmt_size(size: Decimal | float | str) -> str:
    value = Decimal(str(size))
    return f"{value.normalize():f}" if value != value.to_integral_value() else str(int(value))


def fmt_sizes(sizes: Iterable[Decimal]) -> str:
    return ", ".join(fmt_size(s) for s in sizes)


def demo_mark(product: Product) -> str:
    return " <i>(демонстраційний товар)</i>" if product.is_demo else ""


def available_sizes(product: Product) -> list[Decimal]:
    return [s.size for s in product.sizes if s.available > 0]


def product_line(index: int | None, product: Product, *, with_sizes: bool = True) -> str:
    prefix = f"{index}. " if index is not None else ""
    line = f"{prefix}<b>{h(product.title)}</b> — {fmt_price(product.price)}"
    if product.color:
        line += f" ({h(product.color)})"
    if with_sizes:
        sizes = available_sizes(product)
        line += f"\n   Розміри в наявності: {fmt_sizes(sizes)}" if sizes else "\n   Зараз немає в наявності"
    return line + demo_mark(product)


def product_card(product: Product) -> str:
    parts = [f"👟 <b>{h(product.title)}</b>{demo_mark(product)}", f"Ціна: <b>{fmt_price(product.price)}</b>"]
    if product.color:
        parts.append(f"Колір: {h(product.color)}")
    if product.description:
        parts.append(h(product.description))
    sizes = available_sizes(product)
    parts.append(f"Розміри в наявності: {fmt_sizes(sizes)}" if sizes else "На жаль, зараз немає в наявності.")
    return "\n".join(parts)


def product_list(products: Sequence[Product], *, with_sizes: bool = True) -> str:
    return "\n".join(product_line(i, p, with_sizes=with_sizes) for i, p in enumerate(products, start=1))


# ---------------------------------------------------------------- client texts

GREETING = (
    "Вітаю! 👟 Я консультант магазину кросівок. Допоможу підібрати модель, перевірю наявність "
    "потрібного розміру й оформлю замовлення.\n\n"
    "Напишіть бренд, модель, розмір, колір чи бюджет — або просто надішліть фото кросівок."
)
HELP_CLIENT = (
    "Що я вмію:\n"
    "• підібрати кросівки за брендом, моделлю, кольором або бюджетом;\n"
    "• перевірити наявність конкретного розміру;\n"
    "• знайти модель за фото;\n"
    "• запропонувати схожі варіанти;\n"
    "• оформити замовлення.\n\n"
    "Просто напишіть, що шукаєте. Команди: /my_orders — мої замовлення, /forget — видалити мої дані діалогу."
)
ASK_WHICH_MODEL = "Яку модель шукаєте? Можу запропонувати популярні варіанти з каталогу."
ASK_SIZE = "Який розмір вам потрібен?"
FALLBACK_MODE_NOTE = "Зараз працюю у спрощеному режимі, тож відповідаю коротше."
NOT_UNDERSTOOD = (
    "Вибачте, не зовсім зрозумів запит. Напишіть, будь ласка, бренд або модель кросівок, "
    "розмір чи надішліть фото — і я все перевірю."
)
OFF_TOPIC = "Я допомагаю лише з вибором і замовленням кросівок у нашому магазині. Що підібрати для вас? 👟"
NO_EXACT_MATCH = "Точного збігу в нашому каталозі не знайшов."
NO_ALTERNATIVES = (
    "На жаль, зараз немає схожих моделей у потрібному розмірі. "
    "Можу повідомити вам, щойно з'явиться підходящий варіант."
)
WAITLIST_SAVED = "Готово! Я повідомлю вам тут, у Telegram, щойно товар з'явиться в наявності. 🔔"
CHOOSE_PROMPT = "Яка модель вас цікавить?"
ALT_QUESTION = "Які моделі вас цікавлять?"
ERROR_GENERIC = "Вибачте, сталася технічна помилка. Спробуйте, будь ласка, ще раз за хвилину."
ERROR_DB = "Вибачте, зараз не вдається перевірити каталог. Спробуйте, будь ласка, трохи пізніше."
RATE_LIMITED = "Ви надсилаєте повідомлення занадто часто. Зачекайте, будь ласка, хвилинку. 🙏"
ADMIN_ONLY = "Ця команда доступна лише адміністраторам магазину."

PHOTO_TOO_BIG = "Фото завелике. Надішліть, будь ласка, зображення до {mb} МБ."
PHOTO_BAD_TYPE = "Підтримуються лише фото у форматах JPEG, PNG або WEBP."
PHOTO_NOT_SNEAKER = "На фото не бачу кросівок. Надішліть, будь ласка, чіткіше фото взуття або напишіть назву моделі."
PHOTO_UNSURE = (
    "Не можу впевнено визначити модель за цим фото. Надішліть, будь ласка, фото збоку при гарному "
    "освітленні або напишіть назву моделі."
)
PHOTO_AI_DOWN = (
    "Зараз не вдається проаналізувати фото. Напишіть, будь ласка, назву моделі — "
    "і я перевірю наявність у каталозі."
)
PHOTO_DISCLAIMER = "Фото допомагає визначити модель, а наявність я перевіряю в нашій базі."

# Checkout
ORDER_ASK_NAME = "Чудово! Щоб оформити замовлення, напишіть, будь ласка, ваше ім'я."
ORDER_BAD_NAME = "Вкажіть, будь ласка, ім'я літерами (2–60 символів)."
ORDER_ASK_CONTACT = (
    "Дякую, {name}! Тепер залиште номер телефону для зв'язку — натисніть кнопку нижче "
    "або напишіть номер (можна також @username чи e-mail)."
)
ORDER_BAD_CONTACT = "Не вдалося розпізнати контакт. Напишіть номер у форматі +380XXXXXXXXX, @username або e-mail."
ORDER_FOREIGN_CONTACT = "Будь ласка, поділіться власним контактом або напишіть номер вручну."
ORDER_CANCELLED_DRAFT = "Оформлення скасовано. Якщо передумаєте — просто напишіть, що шукаєте. 👟"
ORDER_SIZE_GONE = "На жаль, поки ми оформлювали замовлення, розмір {size} моделі {title} закінчився."
ORDER_DRAFT_MISSING = "Це замовлення вже неактуальне. Напишіть, будь ласка, що бажаєте замовити."
ORDER_ALREADY_CREATED = "Замовлення #{number} вже оформлено. Дякуємо! 👟"
ORDER_TOO_MANY = "Схоже, у вас кілька незавершених замовлень. Дочекайтеся, будь ласка, зв'язку з адміністратором."


def order_summary(*, title: str, size: Decimal, price: Decimal, name: str, contact: str, is_demo: bool) -> str:
    text = (
        "Перевірте, будь ласка, замовлення:\n"
        f"Модель: <b>{h(title)}</b>\n"
        f"Розмір: <b>{fmt_size(size)}</b>\n"
        f"Ціна: <b>{fmt_price(price)}</b>\n"
        f"Ім'я: {h(name)}\n"
        f"Контакт: {h(contact)}\n"
    )
    if is_demo:
        text += "<i>Увага: це демонстраційний товар.</i>\n"
    return text + "\nУсе правильно?"


def order_created(order: Order) -> str:
    item = order.items[0]
    return (
        "Дякуємо за замовлення! 👟\n"
        f"Номер замовлення: <b>#{order.number}</b>\n"
        f"Модель: {h(item.brand)} {h(item.model)}\n"
        f"Розмір: {fmt_size(item.size)}\n"
        f"Сума: <b>{fmt_price(order.total_amount)}</b>\n\n"
        "Ваше замовлення прийнято. Адміністратор зв'яжеться з вами для уточнення оплати та доставки."
    )


CLIENT_STATUS_MESSAGES: dict[str, str] = {
    OrderStatus.CONFIRMED: "Замовлення #{number} підтверджено ✅ Адміністратор зв'яжеться з вами щодо оплати та доставки.",
    OrderStatus.PAID: "Оплату замовлення #{number} отримано ✅ Готуємо до відправлення.",
    OrderStatus.SHIPPED: "Замовлення #{number} відправлено 🚚 Дякуємо за покупку!",
    OrderStatus.COMPLETED: "Замовлення #{number} завершено. Дякуємо, що обрали нас! 👟",
    OrderStatus.REJECTED: "На жаль, замовлення #{number} відхилено. Якщо маєте питання — напишіть нам.",
    OrderStatus.CANCELLED: "Замовлення #{number} скасовано.",
    OrderStatus.EXPIRED: (
        "Термін резерву для замовлення #{number} минув, тому його скасовано. "
        "Якщо ще бажаєте придбати — напишіть, і я перевірю наявність."
    ),
}


def my_orders(orders: Sequence[Order]) -> str:
    if not orders:
        return "У вас ще немає замовлень."
    lines = ["Ваші замовлення:"]
    for order in orders:
        item = order.items[0] if order.items else None
        desc = f"{h(item.brand)} {h(item.model)}, розмір {fmt_size(item.size)}" if item else ""
        lines.append(
            f"#{order.number} — {desc} — {fmt_price(order.total_amount)} — {STATUS_LABELS.get(order.status, order.status)}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------- admin texts

def admin_order_text(order: Order) -> str:
    item = order.items[0]
    username = f"@{h(order.username)}" if order.username else "—"
    header = "НОВЕ ЗАМОВЛЕННЯ" if order.status == OrderStatus.PENDING else "ЗАМОВЛЕННЯ"
    return (
        f"<b>{header} #{order.number}</b>\n"
        f"Модель: {h(item.brand)} {h(item.model)}\n"
        f"Артикул: {h(item.sku)}\n"
        f"Колір: {h(item.color) or '—'}\n"
        f"Розмір: {fmt_size(item.size)}\n"
        f"Ціна: {fmt_price(item.unit_price)}\n"
        f"Ім'я: {h(order.customer_name)}\n"
        f"Контакт: {h(order.contact)}\n"
        f"Telegram: {username}\n"
        f"ID клієнта: <code>{order.telegram_id}</code>\n"
        f"Статус: <b>{STATUS_LABELS.get(order.status, order.status)}</b>"
    )


ADMIN_HELP = (
    "<b>Адмін-команди</b>\n"
    "/admin — панель керування\n"
    "/add_product — додати товар\n"
    "/edit_product АРТИКУЛ — змінити товар\n"
    "/products [пошук] — каталог\n"
    "/stock [АРТИКУЛ] — залишки\n"
    "/set_stock АРТИКУЛ РОЗМІР КІЛЬКІСТЬ — встановити залишок\n"
    "/orders [статус] — замовлення (pending, confirmed, paid, shipped, completed, rejected, cancelled, expired)\n"
    "/order НОМЕР — деталі замовлення\n"
    "/stats — статистика\n"
    "/export_orders [днів] — експорт замовлень у CSV\n"
    "/cancel — вийти з поточної дії\n"
    "/help — допомога"
)
