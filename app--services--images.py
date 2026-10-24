"""Validation of images received from Telegram before they are sent to Claude."""

from __future__ import annotations

ALLOWED_MEDIA_TYPES = ("image/jpeg", "image/png", "image/webp")


class ImageRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason  # "too_big" | "bad_type"


def sniff_media_type(data: bytes) -> str | None:
    """Detect the real format from magic bytes (the declared MIME type can't be trusted)."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check_declared(file_size: int | None, mime_type: str | None, max_bytes: int) -> None:
    """Cheap checks before downloading the file."""
    if file_size is not None and file_size > max_bytes:
        raise ImageRejected("too_big")
    if mime_type is not None and mime_type not in ALLOWED_MEDIA_TYPES:
        raise ImageRejected("bad_type")


def validate_image(data: bytes, max_bytes: int) -> str:
    """Return the detected media type or raise ImageRejected."""
    if len(data) > max_bytes:
        raise ImageRejected("too_big")
    media_type = sniff_media_type(data)
    if media_type is None:
        raise ImageRejected("bad_type")
    return media_type
