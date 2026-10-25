"""Validation of client-provided checkout data."""

from __future__ import annotations

import re

_NAME_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґЁёЫыЭэЪъ' ’ʼ.-]{2,60}$")
_USERNAME_RE = re.compile(r"^@[A-Za-z0-9_]{5,32}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}$")


def normalize_name(raw: str) -> str | None:
    name = " ".join(raw.strip().split())
    if not _NAME_RE.match(name) or not any(ch.isalpha() for ch in name):
        return None
    return name


_WORD_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґЁёЫыЭэЪъ'’ʼ-]{2,40}$")
_CITY_RE = re.compile(r"^[A-Za-zА-Яа-яІіЇїЄєҐґЁёЫыЭэЪъ0-9'’ʼ .,()-]{2,80}$")

DELIVERY_SERVICES = ("nova_poshta", "ukrposhta", "meest")
_SETTLEMENT_PREFIXES = {"м", "с", "смт", "сел", "селище"}


def _capitalize(word: str) -> str:
    return "-".join(part[:1].upper() + part[1:].lower() for part in word.split("-"))


def normalize_full_name(raw: str) -> str | None:
    """Прізвище Ім'я По батькові: 2-4 words of letters (patronymic may be absent, e.g. for foreigners)."""
    words = raw.strip().split()
    if not 2 <= len(words) <= 4 or not all(_WORD_RE.match(word) for word in words):
        return None
    return " ".join(_capitalize(word) for word in words)


def first_name(full_name: str) -> str:
    """Ім'я from "Прізвище Ім'я По батькові" (used to address the client)."""
    parts = full_name.split()
    return parts[1] if len(parts) >= 2 else full_name


def normalize_city(raw: str) -> str | None:
    city = " ".join(raw.strip().split())
    if not _CITY_RE.match(city) or sum(ch.isalpha() for ch in city) < 2:
        return None
    first = city.split()[0].rstrip(".").lower()
    if first in _SETTLEMENT_PREFIXES:  # "смт. Козелець", "с. Петрівка" stay as typed
        return city
    return city[:1].upper() + city[1:]


def normalize_delivery_point(raw: str, service: str) -> str | None:
    """Branch / parcel locker number (Nova Poshta, Meest) or postcode + address (Ukrposhta)."""
    point = " ".join(raw.strip().split())
    if not 1 <= len(point) <= 160:
        return None
    if service == "ukrposhta":
        return point if re.search(r"(?<!\d)\d{5}(?!\d)", point) else None
    if not re.search(r"\d", point):
        return None
    if point.isdigit():
        return f"Відділення №{point}"
    return point


def normalize_phone(raw: str) -> str | None:
    """Accept Ukrainian and international numbers. Returns E.164-like '+380XXXXXXXXX' or None."""
    digits = re.sub(r"[\s()\-.]", "", raw.strip())
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if re.fullmatch(r"0\d{9}", digits):          # 0671234567
        return "+38" + digits
    if re.fullmatch(r"380\d{9}", digits):        # 380671234567
        return "+" + digits
    if re.fullmatch(r"\+\d{10,15}", digits):
        return digits
    return None


def normalize_contact(raw: str) -> str | None:
    """Phone number, Telegram @username or e-mail."""
    text = raw.strip()
    phone = normalize_phone(text)
    if phone:
        return phone
    if _USERNAME_RE.match(text):
        return text
    if _EMAIL_RE.match(text) and len(text) <= 64:
        return text.lower()
    return None


_PHONE_IN_TEXT = re.compile(r"(?<![\d+])(?:\+?\d{1,3}[\s-]?)?\(?\d{2,3}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}(?!\d)|\+?\d{10,15}")
_EMAIL_IN_TEXT = re.compile(r"[^@\s]{1,64}@[^@\s]+\.[A-Za-z]{2,}")


def mask_personal_data(text: str) -> str:
    """Remove phone numbers and e-mails before sending text to the AI provider or logs."""
    text = _PHONE_IN_TEXT.sub("[телефон]", text)
    return _EMAIL_IN_TEXT.sub("[email]", text)
