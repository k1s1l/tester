from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import Settings
from app.logging_setup import redact
from app.schemas.catalog import ProductIn, parse_size, parse_sizes_spec
from app.schemas.order import mask_personal_data, normalize_contact, normalize_name, normalize_phone
from app.services.fallback_parser import parse_message
from app.services.reports import _csv_safe


def test_phone_normalisation():
    assert normalize_phone("067 123 45 67") == "+380671234567"
    assert normalize_phone("+38 (067) 123-45-67") == "+380671234567"
    assert normalize_phone("380671234567") == "+380671234567"
    assert normalize_phone("12345") is None
    assert normalize_contact("@nikita_shop") == "@nikita_shop"
    assert normalize_contact("Me@Mail.com") == "me@mail.com"
    assert normalize_contact("drop table orders;") is None


def test_name_validation():
    assert normalize_name("  Микита  ") == "Микита"
    assert normalize_name("Анна-Марія О'Ніл") == "Анна-Марія О'Ніл"
    assert normalize_name("1234") is None
    assert normalize_name("<script>") is None


def test_sizes():
    assert parse_size("42,5") == Decimal("42.5")
    assert parse_size("EU 43") == Decimal("43.0")
    with pytest.raises(ValueError):
        parse_size("42.3")
    with pytest.raises(ValueError):
        parse_size("99")
    rows = parse_sizes_spec("42:3, 43:1; 44.5=0")
    assert [(r.size, r.quantity) for r in rows] == [(Decimal("42.0"), 3), (Decimal("43.0"), 1), (Decimal("44.5"), 0)]
    with pytest.raises(ValueError):
        parse_sizes_spec("42 three")


def test_product_validation():
    with pytest.raises(ValueError):
        ProductIn(brand="Nike", model="X", sku="bad sku!", price=Decimal("10"))
    with pytest.raises(ValueError):
        ProductIn(brand="Nike", model="X", sku="OK1", price=Decimal("-1"))
    assert ProductIn(brand="Nike", model="X", sku="ok-1", price=Decimal("10")).sku == "OK-1"


def test_masking_and_log_redaction():
    assert "0671234567" not in mask_personal_data("тел 0671234567")
    assert "42 43 44 45" in mask_personal_data("розміри 42 43 44 45")
    line = "token 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw key sk-ant-api03-abc postgresql://u:secret@host/db"
    cleaned = redact(line)
    assert "AAHdq" not in cleaned and "sk-ant-api03" not in cleaned and "secret" not in cleaned


def test_csv_injection_protection():
    assert _csv_safe("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_safe("+380671234567") == "'+380671234567"
    assert _csv_safe("Микита") == "Микита"


def test_settings_parsing():
    s = Settings(_env_file=None, bot_token="1:x", database_url="postgres://u:p@h:5432/d", admin_ids="1, 2;3")  # type: ignore[call-arg]
    assert s.admin_ids == [1, 2, 3]
    assert s.database_url == "postgresql+asyncpg://u:p@h:5432/d"
    assert s.admin_notification_chats == [1, 2, 3]
    assert s.is_admin(2) and not s.is_admin(4) and not s.is_admin(None)


def test_fallback_parser_basic(settings):
    class P:
        def __init__(self, brand, model):
            self.brand, self.model = brand, model

    products = [P("Nike", "Vomero 5"), P("ASICS", "GEL-1130"), P("ASICS", "Gel Kayano 31")]
    a = parse_message("Хочу найк вомеро 5 47 розмір", products)  # type: ignore[arg-type]
    assert (a.brand, a.size) == ("Nike", "47")
    a = parse_message("nike vomero 5 42,5", products)  # type: ignore[arg-type]
    assert (a.model, a.size, a.intent) == ("Vomero 5", "42.5", "search")
    a = parse_message("asics gel kayano 31", products)  # type: ignore[arg-type]
    assert a.model == "Gel Kayano 31" and a.size is None
    a = parse_message("хочу замовити gel-1130 43", products)  # type: ignore[arg-type]
    assert (a.intent, a.model, a.size) == ("start_order", "GEL-1130", "43")
