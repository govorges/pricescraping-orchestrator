from __future__ import annotations

from orchestrator.classify import classify_listing_type


def test_auction_from_subtitle_text():
    """A 'bids · time left' subtitle is a clear auction signal."""
    assert classify_listing_type("RTX 5090", {"subtitle": "3 bids · 2d 4h left"}) == "auction"


def test_auction_from_bid_count_variants():
    """'0 bids' still counts (an auction with no bids yet), and phrasing/casing/field-name variants all match."""
    assert classify_listing_type("RTX 5090", {"sub": "0 bids"}) == "auction"
    assert classify_listing_type("RTX 5090", {"sub": "27 Bids"}) == "auction"
    assert classify_listing_type("RTX 5090 (Ends Sat, 10:30)", {}) == "auction"


def test_buy_now_from_subtitle_text():
    """'Buy It Now' / 'or Best Offer' / a struck-through 'was' price are all fixed-price signals."""
    assert classify_listing_type("RTX 5090", {"subtitle": "Buy It Now"}) == "buy_now"
    assert classify_listing_type("RTX 5090", {"subtitle": "or Best Offer"}) == "buy_now"
    assert classify_listing_type("RTX 5090", {"price_was": "Was $2,199 (9% off)"}) == "buy_now"


def test_no_auction_signal_defaults_to_buy_now():
    """A fixed-price-only vendor surfaces no format text at all — that is still buy_now, never "unknown"."""
    assert classify_listing_type("NVIDIA GeForce RTX 5090 Founders Edition", {}) == "buy_now"
    assert classify_listing_type("Ryzen 7 9800X3D", {"shipping": "Free", "condition": "New"}) == "buy_now"


def test_unknown_only_when_nothing_to_go_on():
    """No title and no extra fields at all is the only case that falls back to 'unknown'."""
    assert classify_listing_type(None, None) == "unknown"
    assert classify_listing_type("", {}) == "unknown"


def test_explicit_field_wins():
    """A vendor-provided listing_format/sale_type/buying_format field overrides any text-based guess."""
    assert classify_listing_type("Widget", {"listing_format": "auction"}) == "auction"
    assert classify_listing_type("Widget", {"sale_type": "Fixed Price"}) == "buy_now"
    assert classify_listing_type("bidding war widget", {"buying_format": "fixed-price"}) == "buy_now"


def test_auction_marker_in_title_itself():
    """An auction cue can live in the title text alone, with no separate subtitle/extra field."""
    assert classify_listing_type("RTX 5090 - current bid $500", {}) == "auction"
