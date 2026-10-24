"""Scenarios 6-7: photo search and unclear photos; image validation."""

from __future__ import annotations

import pytest

from app.schemas.ai import PhotoAnalysis
from app.services.ai import AIUnavailable
from app.services.images import ImageRejected, check_declared, validate_image

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


# 6. Client sends a photo of a model that is in the catalog
async def test_photo_recognised_and_stock_checked(assistant, fake_ai, user, ctx):
    fake_ai.photo_result = PhotoAnalysis(
        is_sneaker=True, brand="Nike", model="Vomero 5", colors=["сірий"], confidence=0.9, reply="Сірі кросівки"
    )
    reply = await assistant.handle_photo(user, JPEG, "image/jpeg", "є 47?", ctx)
    assert "Схоже, на фото <b>Nike Vomero 5</b>" in reply.text
    assert "наявність я перевіряю в нашій базі" in reply.text
    assert "є в наявності" in reply.text and "5800 грн" in reply.text
    assert ctx.size == "47"


async def test_photo_model_not_in_catalog_offers_similar(assistant, fake_ai, user, ctx):
    fake_ai.photo_result = PhotoAnalysis(
        is_sneaker=True, brand="Nike", model="Air Force 1", colors=["білий"], confidence=0.95, reply="Білі кросівки"
    )
    reply = await assistant.handle_photo(user, JPEG, "image/jpeg", None, ctx)
    assert "Точного збігу в нашому каталозі не знайшов" in reply.text
    assert "Air Force 1" in reply.text
    assert "Nike" in reply.text  # similar Nike models that are really in stock


# 7. Photo does not allow identifying the model
async def test_unclear_photo_asks_for_more(assistant, fake_ai, user, ctx):
    fake_ai.photo_result = PhotoAnalysis(is_sneaker=True, brand="Nike", model=None, confidence=0.3, reply="Розмите фото")
    reply = await assistant.handle_photo(user, JPEG, "image/jpeg", None, ctx)
    assert "Не можу впевнено визначити модель" in reply.text
    assert "грн" not in reply.text


async def test_not_a_sneaker(assistant, fake_ai, user, ctx):
    fake_ai.photo_result = PhotoAnalysis(is_sneaker=False, confidence=0.9, reply="Кіт")
    reply = await assistant.handle_photo(user, PNG, "image/png", None, ctx)
    assert "не бачу кросівок" in reply.text


async def test_photo_when_ai_down(assistant, fake_ai, user, ctx):
    fake_ai.photo_result = AIUnavailable("overloaded")
    reply = await assistant.handle_photo(user, JPEG, "image/jpeg", None, ctx)
    assert "назву моделі" in reply.text


def test_image_validation():
    assert validate_image(JPEG, 1000) == "image/jpeg"
    assert validate_image(PNG, 1000) == "image/png"
    with pytest.raises(ImageRejected) as exc:
        validate_image(b"GIF89a....", 1000)
    assert exc.value.reason == "bad_type"
    with pytest.raises(ImageRejected) as exc:
        validate_image(JPEG, 10)
    assert exc.value.reason == "too_big"
    with pytest.raises(ImageRejected):
        check_declared(10_000_000, "image/jpeg", 5_000_000)
    with pytest.raises(ImageRejected):
        check_declared(100, "application/pdf", 5_000_000)
