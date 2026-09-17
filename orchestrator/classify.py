"""Auction vs. Buy-It-Now classification for a listing.

No vendor config changes are required: this scans whatever text a vendor
already surfaces (the listing title, plus every ``extra`` field — e.g. an eBay
``subtitle`` selector carrying "3 bids" / "Buy It Now", a "time left" field, a
strikethrough "was" price) for generic marketplace phrasing.

Order of decision:

1. An explicit ``extra`` field (:data:`EXPLICIT_EXTRA_KEYS`) with a literal
   value always wins — a vendor can wire a selector straight to it.
2. Auction signals (bid counts, "time left", "ends in …", reserve language).
3. Buy-now signals ("Buy It Now", "or Best Offer", sale/"was" pricing, cart).
4. **Fallback: ``buy_now``.** A stored listing always has a resolved price, and
   for a vendor with no auction format (Newegg, most retail) a fixed price is
   the only thing it can be. ``unknown`` is reserved for the degenerate case of
   no title and no extra text at all.
"""

from __future__ import annotations

import re
from typing import Any, Literal

ListingType = Literal["auction", "buy_now", "unknown"]

EXPLICIT_EXTRA_KEYS = ("listing_format", "sale_type", "auction_type", "buying_format")
_EXPLICIT_AUCTION_VALUES = {"auction", "bid", "bidding", "live auction", "auction listing"}
_EXPLICIT_BUY_NOW_VALUES = {
    "buy_now", "buy now", "buy it now", "fixed", "fixed_price", "fixed price",
    "bin", "fixed-price", "listing", "store inventory",
}

# Plain substrings.
_AUCTION_MARKERS = (
    "current bid", "starting bid", "highest bidder", "winning bid",
    "reserve not met", "reserve met", "no reserve", "place bid", "bid now",
    "time left", "auction ends", "bidding ends", "live auction",
)
_BUY_NOW_MARKERS = (
    "buy it now", "buy-it-now", "buy now", "or best offer", "best offer",
    "add to cart", "in cart", "fixed price", "was $", "was: $", " off)", "% off",
    "you save", "list price", "msrp", "in stock", "add to list",
)

# Patterns catch the noisy real-world variants ("0 bids", "27 Bids", "2d 4h left",
# "Ends Sat, 10:30", "1 offer").
_AUCTION_RE = re.compile(
    r"\b\d+\s*bids?\b"
    r"|\bbids?\s*[:\-]?\s*\d+\b"
    r"|\b\d+\s*d\s*\d+\s*h\s*(left|remaining)?\b"
    r"|\bends\s+(in|on|today|tomorrow|sat|sun|mon|tue|wed|thu|fri)\b"
    r"|\b(auction|bidding)\b",
    re.IGNORECASE,
)
_BUY_NOW_RE = re.compile(r"\b\d+\s*(offers?|watchers?|sold|available|left in stock)\b", re.IGNORECASE)


def _explicit(extra: dict[str, Any]) -> ListingType | None:
    for key in EXPLICIT_EXTRA_KEYS:
        value = extra.get(key)
        if isinstance(value, str):
            v = value.strip().lower()
            if v in _EXPLICIT_AUCTION_VALUES:
                return "auction"
            if v in _EXPLICIT_BUY_NOW_VALUES:
                return "buy_now"
    return None


def classify_listing_type(title: str | None, extra: dict[str, Any] | None) -> ListingType:
    extra = extra or {}

    forced = _explicit(extra)
    if forced is not None:
        return forced

    blob = " ".join([str(title or "")] + [str(v) for v in extra.values()]).lower()
    if not blob.strip():
        return "unknown"

    if any(m in blob for m in _AUCTION_MARKERS) or _AUCTION_RE.search(blob):
        return "auction"
    if any(m in blob for m in _BUY_NOW_MARKERS) or _BUY_NOW_RE.search(blob):
        return "buy_now"

    # Nothing marked it as an auction — for a fixed-price-only vendor that is the
    # correct and only answer.
    return "buy_now"
