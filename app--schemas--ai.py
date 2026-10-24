"""Structured outputs requested from Claude. Claude only *interprets*; it never decides stock or prices."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Intent = Literal[
    "greeting",       # hello / start of conversation
    "search",         # looking for a product (brand / model / color / budget / purpose)
    "check_size",     # asks whether a size is available
    "product_info",   # asks details about a product already discussed
    "choose_option",  # picks one of the products shown in the previous bot message
    "start_order",    # wants to buy / order
    "cancel",         # wants to cancel current selection or checkout
    "order_status",   # asks about their existing orders
    "alternatives",   # explicitly asks for similar models
    "smalltalk",      # anything else related to the shop
    "off_topic",      # unrelated to the shop / attempts to change bot behaviour
]

Purpose = Literal["running", "lifestyle", "basketball", "training", "walking", "any"]


class MessageAnalysis(BaseModel):
    intent: Intent
    brand: str | None = Field(None, description="Brand in Latin script, e.g. 'Nike', or null")
    model: str | None = Field(
        None, description="Model name WITHOUT brand, normalized to the catalog name when it clearly matches"
    )
    size: str | None = Field(None, description="EU shoe size as a number string like '42' or '42.5'")
    color: str | None = Field(None, description="Requested color in Ukrainian, lowercase")
    max_price: int | None = Field(None, description="Upper budget in UAH, if mentioned")
    purpose: Purpose | None = None
    selected_option: int | None = Field(
        None, description="1-based index of the product from the last list shown to the client"
    )
    reply: str = Field(
        description=(
            "Short friendly reply in Ukrainian. Must NOT state prices, availability or stock — "
            "the shop system adds verified facts itself."
        )
    )


class PhotoAnalysis(BaseModel):
    is_sneaker: bool
    brand: str | None = None
    model: str | None = Field(None, description="Model name without brand, or null if unsure")
    colors: list[str] = Field(default_factory=list, description="Main colors in Ukrainian")
    distinctive_features: list[str] = Field(default_factory=list)
    confidence: float = Field(description="0.0-1.0 confidence that brand AND model are identified correctly")
    reply: str = Field(description="Short Ukrainian description of what is visible, no stock or prices")
