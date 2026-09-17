"""Canonicalise a listing URL so the same real-world item can be recognised
across scrapes even though the query string carries session/tracking noise
(``?epid=...&hash=...&_trksid=...``) that changes on every visit.

Strips the query string and fragment, lower-cases scheme/host, and drops a
trailing slash. Two URLs that only differ by query string or fragment collapse
to the same canonical form; anything else (path, host) still distinguishes them.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def canonical_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))
