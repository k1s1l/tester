"""Catalog import from a Telegram channel: forwarded old posts, new channel posts, drafts, albums."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import count

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import Chat, Message, MessageOriginChannel, PhotoSize, Update, User
from sqlalchemy import select

from app.database.models import DraftStatus, Notification, Product, ProductDraft, WaitlistRequest
from app.keyboards.admin import DraftCb
from app.schemas.ai import CatalogPostAnalysis, ParsedProduct, ParsedSize
from app.services.ai import AIUnavailable
from app.services.channel_import import build_payload
from tests.conftest import ADMIN_CHAT, ADMIN_ID, CLIENT_ID

CHANNEL = Chat(id=-1009876543210, type="channel", title="Sneaker Drops")
_ids = count(5000)


@pytest.fixture(autouse=True)
def _reset(session_mock, module_ai, settings):
    session_mock.clear()
    session_mock.fail_with = None
    module_ai.post_result = None
    module_ai.message_result = None
    module_ai.calls.clear()
    settings.channel_auto_publish = False
    yield
    settings.channel_auto_publish = False


def product(model: str = "Air Max 90", price: int | None = 4500, sizes=(("40", None), ("41", None)), color: str = "білий"):
    return ParsedProduct(
        brand="Nike",
        model=model,
        color=color,
        category="lifestyle",
        price_uah=price,
        sizes=[ParsedSize(size=s, quantity=q) for s, q in sizes],
        description="Класика на кожен день.",
    )


def post_analysis(*products: ParsedProduct, is_product: bool = True) -> CatalogPostAnalysis:
    return CatalogPostAnalysis(is_product_post=is_product, products=list(products), confidence=0.9)


def photo(n: int) -> list[PhotoSize]:
    return [PhotoSize(file_id=f"photo-{n}", file_unique_id=f"u{n}", width=800, height=800)]


def forwarded(msg_id: int, *, caption: str | None = None, group: str | None = None, n_photo: int = 1, sender: int = ADMIN_ID) -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(
            message_id=next(_ids),
            date=datetime.now(),
            chat=Chat(id=sender, type="private"),
            from_user=User(id=sender, is_bot=False, first_name="Owner"),
            forward_origin=MessageOriginChannel(type="channel", date=datetime.now(), chat=CHANNEL, message_id=msg_id),
            photo=photo(n_photo),
            caption=caption,
            media_group_id=group,
        ),
    )


def channel_post(msg_id: int, caption: str) -> Update:
    return Update(
        update_id=next(_ids),
        channel_post=Message(message_id=msg_id, date=datetime.now(), chat=CHANNEL, photo=photo(msg_id), caption=caption),
    )


def callback(data: str, user: int = ADMIN_ID) -> Update:
    return Update(
        update_id=next(_ids),
        callback_query={
            "id": str(next(_ids)),
            "from": {"id": user, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "x",
            "data": data,
            "message": {"message_id": 1, "date": int(datetime.now().timestamp()), "chat": {"id": user, "type": "private"}, "text": "x"},
        },
    )


def text_message(text: str, user: int = ADMIN_ID) -> Update:
    return Update(
        update_id=next(_ids),
        message=Message(
            message_id=next(_ids), date=datetime.now(), chat=Chat(id=user, type="private"),
            from_user=User(id=user, is_bot=False, first_name="Owner"), text=text,
        ),
    )


async def feed(dispatcher, bot, *updates: Update) -> None:
    for update in updates:
        await dispatcher.feed_update(bot, update)
    await dispatcher["importer"].wait_idle()


def draft_cards(session_mock, chat_id: int) -> list[SendPhoto | SendMessage]:
    return [
        r for r in session_mock.requests
        if isinstance(r, (SendPhoto, SendMessage)) and int(r.chat_id) == chat_id
        and "Товар із каналу" in ((r.caption if isinstance(r, SendPhoto) else r.text) or "")
    ]


def buttons(card) -> list[str]:
    return [b.callback_data for row in card.reply_markup.inline_keyboard for b in row]


def answers(session_mock) -> list[str]:
    return [r.text or "" for r in session_mock.requests if isinstance(r, AnswerCallbackQuery)]


async def test_forwarded_album_becomes_one_draft_and_is_saved(dispatcher, bot, session_mock, module_ai, session_factory):
    module_ai.post_result = post_analysis(product())
    await feed(
        dispatcher, bot,
        forwarded(101, caption="Nike Air Max 90 білі\n40-41\n4500 грн", group="album1", n_photo=1),
        forwarded(102, group="album1", n_photo=2),
        forwarded(103, group="album1", n_photo=3),
    )
    assert len([c for c in module_ai.calls if "text" in c and "image" in c]) == 1  # one Claude call for the album
    assert "4500 грн" in module_ai.calls[-1]["text"]
    cards = draft_cards(session_mock, ADMIN_ID)
    assert len(cards) == 1
    card = cards[0]
    assert isinstance(card, SendPhoto) and card.photo == "photo-1"
    assert "Nike Air Max 90" in card.caption and "4500 грн" in card.caption and "Фото: 3" in card.caption
    assert "Кількість пар у пості не вказана" in card.caption

    save = next(b for b in buttons(card) if DraftCb.unpack(b).action == "new")
    session_mock.clear()
    await feed(dispatcher, bot, callback(save))
    assert answers(session_mock) == ["Збережено ✅"]
    async with session_factory() as session:
        saved = await session.scalar(select(Product).where(Product.model == "Air Max 90"))
        draft = await session.scalar(select(ProductDraft))
    assert saved is not None and not saved.is_demo and saved.is_active and saved.price == Decimal("4500.00")
    assert saved.sku == "NIKE-AIR-MAX-90"
    assert draft.status == DraftStatus.SAVED and draft.product_id == saved.id
    from app.services.catalog import get_product

    async with session_factory() as session:
        full = await get_product(session, saved.id)
    assert [(str(s.size), s.quantity) for s in full.sizes] == [("40.0", 1), ("41.0", 1)]
    assert [p.telegram_file_id for p in full.photos] == ["photo-1", "photo-2", "photo-3"]

    # second press does not create a duplicate
    session_mock.clear()
    await feed(dispatcher, bot, callback(save))
    assert answers(session_mock) == ["Цю чернетку вже збережено."]
    async with session_factory() as session:
        assert len((await session.scalars(select(Product).where(Product.model == "Air Max 90"))).all()) == 1


async def test_same_post_forwarded_twice(dispatcher, bot, session_mock, module_ai):
    module_ai.post_result = post_analysis(product())
    await feed(dispatcher, bot, forwarded(201, caption="Nike Air Max 90 4500"))
    session_mock.clear()
    await feed(dispatcher, bot, forwarded(201, caption="Nike Air Max 90 4500"))
    assert session_mock.sent_texts(ADMIN_ID) == ["ℹ️ Цей пост уже оброблено раніше."]


async def test_new_channel_post_updates_existing_product_and_notifies_waitlist(
    dispatcher, bot, session_mock, module_ai, session_factory
):
    # 1) first drop creates the product (size 42 sold out: 0 pairs)
    module_ai.post_result = post_analysis(product(sizes=(("41", 2), ("42", 0))))
    await feed(dispatcher, bot, channel_post(301, "Nike Air Max 90, 41 (2 пари), 4500"))
    first = draft_cards(session_mock, ADMIN_CHAT)[0]  # channel drafts go to ADMIN_CHAT_ID
    await feed(dispatcher, bot, callback(next(b for b in buttons(first) if DraftCb.unpack(b).action == "new")))
    async with session_factory() as session:
        prod = await session.scalar(select(Product).where(Product.model == "Air Max 90"))
        session.add(WaitlistRequest(telegram_id=CLIENT_ID, product_id=prod.id, size=Decimal("42")))
        await session.commit()

    # 2) new drop: size 42 is back, new price
    session_mock.clear()
    module_ai.post_result = post_analysis(product(price=4700, sizes=(("42", 3),)))
    await feed(dispatcher, bot, channel_post(302, "Новий дроп! Nike Air Max 90, 42 — 3 пари, 4700 грн"))
    card = draft_cards(session_mock, ADMIN_CHAT)[0]
    text = card.caption if isinstance(card, SendPhoto) else card.text
    assert "Схожий товар уже є" in text and "NIKE-AIR-MAX-90" in text
    update_btn = next(b for b in buttons(card) if DraftCb.unpack(b).action == "update")
    await feed(dispatcher, bot, callback(update_btn))

    from app.services.catalog import get_product

    async with session_factory() as session:
        prod = await get_product(session, prod.id)
        notes = (await session.scalars(select(Notification).where(Notification.kind == "client_restock"))).all()
        count_products = len((await session.scalars(select(Product).where(Product.model == "Air Max 90"))).all())
    assert count_products == 1
    assert prod.price == Decimal("4700.00")
    assert {str(s.size): s.quantity for s in prod.sizes} == {"41.0": 2, "42.0": 3}
    assert len(notes) == 1 and notes[0].chat_id == CLIENT_ID


async def test_missing_price_must_be_entered(dispatcher, bot, session_mock, module_ai, session_factory):
    module_ai.post_result = post_analysis(product(model="Dunk Low", price=None, color="чорний"))
    await feed(dispatcher, bot, forwarded(401, caption="Nike Dunk Low чорні, 40-41. Ціна в дірект"))
    card = draft_cards(session_mock, ADMIN_ID)[0]
    assert "Ціну в пості не знайдено" in card.caption
    draft_id = DraftCb.unpack(buttons(card)[0]).draft_id

    session_mock.clear()
    await feed(dispatcher, bot, callback(DraftCb(action="new", draft_id=draft_id).pack()))
    assert "вкажіть ціну" in answers(session_mock)[0]

    await feed(dispatcher, bot, callback(DraftCb(action="price", draft_id=draft_id).pack()))
    session_mock.clear()
    await feed(dispatcher, bot, text_message("3 900 грн"))
    card = draft_cards(session_mock, ADMIN_ID)[0]
    assert "3900 грн" in card.caption

    await feed(dispatcher, bot, callback(DraftCb(action="sizes", draft_id=draft_id).pack()))
    session_mock.clear()
    await feed(dispatcher, bot, text_message("40:2, 41:1"))
    card = draft_cards(session_mock, ADMIN_ID)[0]
    assert "40 (2 шт.), 41 (1 шт.)" in card.caption and "не вказана" not in card.caption

    await feed(dispatcher, bot, callback(DraftCb(action="new", draft_id=draft_id).pack()))
    async with session_factory() as session:
        saved = await session.scalar(select(Product).where(Product.model == "Dunk Low"))
    assert saved is not None and saved.price == Decimal("3900.00")


async def test_skip_and_non_product_posts(dispatcher, bot, session_mock, module_ai, session_factory):
    module_ai.post_result = post_analysis(is_product=False)
    await feed(dispatcher, bot, forwarded(501, caption="Розіграш! Підписуйтесь"))
    assert session_mock.sent_texts(ADMIN_ID) == ["ℹ️ Це не схоже на пост із кросівками на продаж — пропускаю."]

    session_mock.clear()
    module_ai.post_result = post_analysis(product(model="Gel Kayano 14", color="сріблястий"))
    await feed(dispatcher, bot, forwarded(502, caption="ASICS..."))
    draft_id = DraftCb.unpack(buttons(draft_cards(session_mock, ADMIN_ID)[0])[0]).draft_id
    await feed(dispatcher, bot, callback(DraftCb(action="skip", draft_id=draft_id).pack()))
    async with session_factory() as session:
        assert (await session.get(ProductDraft, draft_id)).status == DraftStatus.SKIPPED
        assert await session.scalar(select(Product).where(Product.model == "Gel Kayano 14")) is None


async def test_claude_unavailable(dispatcher, bot, session_mock, module_ai):
    module_ai.post_result = AIUnavailable("overloaded")
    await feed(dispatcher, bot, forwarded(601, caption="Nike Air Max 90 4500"))
    assert "Claude зараз недоступний" in session_mock.sent_texts(ADMIN_ID)[0]


async def test_auto_publish(dispatcher, bot, session_mock, module_ai, session_factory, settings):
    settings.channel_auto_publish = True
    module_ai.post_result = post_analysis(product(model="Air Force 1", sizes=(("42", 2),)))
    await feed(dispatcher, bot, channel_post(701, "Nike Air Force 1, 42 — 2 пари, 4500"))
    assert any("автоматично додано" in t for t in session_mock.sent_texts(ADMIN_CHAT))
    async with session_factory() as session:
        saved = await session.scalar(select(Product).where(Product.model == "Air Force 1"))
    assert saved is not None and saved.is_active


async def test_regular_user_cannot_import(dispatcher, bot, session_mock, module_ai, session_factory):
    module_ai.post_result = post_analysis(product(model="Secret Model"))
    await feed(dispatcher, bot, forwarded(801, caption="Nike Secret Model 4500", sender=CLIENT_ID))
    async with session_factory() as session:
        assert (await session.scalars(select(ProductDraft))).all() == []
    session_mock.clear()
    await feed(dispatcher, bot, callback(DraftCb(action="new", draft_id=1).pack(), user=CLIENT_ID))
    assert answers(session_mock) == ["Ця команда доступна лише адміністраторам магазину."]


def test_build_payload_sizes():
    parsed = product(sizes=(("40", None), ("41,5", 2), ("99", 1), ("abc", 1)))
    payload = build_payload(parsed, default_quantity=1)
    assert payload["sizes"] == [
        {"size": "40.0", "quantity": 1, "guessed": True},
        {"size": "41.5", "quantity": 2, "guessed": False},
    ]
    assert payload["price"] == "4500" and payload["sku"] == "NIKE-AIR-MAX-90"
