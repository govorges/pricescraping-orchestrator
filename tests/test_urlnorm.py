from __future__ import annotations

from orchestrator.urlnorm import canonical_url


def test_strips_query_string_and_fragment():
    """Two URLs for the same listing that only differ by session/tracking query args and a fragment canonicalise identically."""
    a = canonical_url("https://www.ebay.com/itm/123?epid=abc&hash=xyz&_trksid=1")
    b = canonical_url("https://www.ebay.com/itm/123?epid=def&hash=qqq&_trksid=2#section")
    assert a == b == "https://www.ebay.com/itm/123"


def test_normalises_case_and_trailing_slash():
    """Scheme/host case and a trailing slash are normalised away, but the path's own case is preserved."""
    assert canonical_url("HTTPS://Example.com/Path/") == "https://example.com/Path"
    assert canonical_url("https://example.com/path") == canonical_url("https://example.com/path/")


def test_distinguishes_different_paths_and_hosts():
    """Genuinely different paths or hosts must never canonicalise to the same identity."""
    assert canonical_url("https://a.com/x") != canonical_url("https://a.com/y")
    assert canonical_url("https://a.com/x") != canonical_url("https://b.com/x")


def test_none_and_malformed_input():
    """None, empty, non-URL, and relative-path input all fail closed to None rather than raising or guessing."""
    assert canonical_url(None) is None
    assert canonical_url("") is None
    assert canonical_url("not a url") is None
    assert canonical_url("/relative/path") is None
