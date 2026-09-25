"""GPU performance data from Tom's Hardware's GPU benchmarks hierarchy.

The hierarchy article carries two tables -- rasterization and ray tracing --
each listing every card's MSRP and average fps at 1080p / 1440p / 4K Ultra
(cells read like ``"90.1% (183.6)"``: relative score, then fps). The parsed
result is cached whole in :class:`~orchestrator.models.BenchmarkSnapshot` and
only re-fetched once it is more than :data:`MAX_AGE` old, so the page that uses
it doesn't hit tomshardware.com on every load. A failed re-fetch falls back to
the stale snapshot rather than blanking the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser

import httpx

from orchestrator.logging import get_logger
from orchestrator.models import BenchmarkSnapshot

log = get_logger("orchestrator.benchmarks")

SOURCE = "tomshardware-gpu-hierarchy"
URL = "https://www.tomshardware.com/reviews/gpu-hierarchy,4388.html"
MAX_AGE = timedelta(hours=24)

MODES = {"raster": "Rasterization", "rt": "Ray tracing"}
RESOLUTIONS = {"1080p": "1080p Ultra", "1440p": "1440p Ultra", "4k": "4K Ultra"}

_HEADERS = {
    # the site serves a stripped page (or a 403) to obviously-scripted clients
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---- parsing --------------------------------------------------------------


class _TableParser(HTMLParser):
    """Collects every ``<table>`` as ``{"caption": str, "rows": [[cell text]]}``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[dict] = []
        self._table: dict | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._caption: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table = {"caption": "", "rows": []}
        elif self._table is None:
            return
        elif tag == "caption":
            self._caption = []
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if self._table is None:
            return
        if tag == "table":
            self.tables.append(self._table)
            self._table = None
        elif tag == "caption" and self._caption is not None:
            self._table["caption"] = _clean("".join(self._caption))
            self._caption = None
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self._table["rows"].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)
        elif self._caption is not None:
            self._caption.append(data)


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(s)).strip()


def _money(s: str) -> float | None:
    m = re.search(r"[\d,]+(?:\.\d+)?", s or "")
    return float(m.group(0).replace(",", "")) if m else None


def _fps(s: str) -> float | None:
    """``"90.1% (183.6)"`` -> 183.6; a bare ``"183.6"`` is taken as fps too."""
    m = re.search(r"\(\s*([\d,]+(?:\.\d+)?)\s*\)", s or "")
    if m:
        return float(m.group(1).replace(",", ""))
    if s and "%" not in s:
        return _money(s)
    return None


def _resolution_of(header: str) -> str | None:
    h = header.lower()
    for key, token in (("1080p", "1080p"), ("1440p", "1440p"), ("4k", "4k")):
        if token in h:
            return key
    return None


def _parse_table(rows: list[list[str]]) -> list[dict]:
    header, body = rows[0], rows[1:]
    res_cols = {i: r for i, h in enumerate(header) if (r := _resolution_of(h))}
    msrp_col = next((i for i, h in enumerate(header) if "msrp" in h.lower()), None)
    out = []
    for r in body:
        if not r or not r[0]:
            continue
        fps = {res: v for i, res in res_cols.items() if i < len(r) and (v := _fps(r[i])) is not None}
        if not fps:
            continue
        msrp = _money(r[msrp_col]) if msrp_col is not None and msrp_col < len(r) else None
        out.append({"name": r[0], "msrp": msrp, "fps": fps})
    return out


def parse_hierarchy(html: str) -> dict[str, list[dict]]:
    """Pull the rasterization and ray-tracing tables out of the article HTML.

    Returns ``{"raster": [...], "rt": [...]}``, each row
    ``{"name", "msrp", "fps": {"1080p": .., "1440p": .., "4k": ..}}``. Tables
    are identified by caption; failing that, the first two tables with
    resolution columns are taken as rasterization then ray tracing (the order
    the article has always used).
    """
    p = _TableParser()
    p.feed(html)
    candidates = [
        t for t in p.tables
        if t["rows"] and any(_resolution_of(h) for h in t["rows"][0])
    ]
    out: dict[str, list[dict]] = {}
    unlabelled = []
    for t in candidates:
        cap = t["caption"].lower()
        if "ray tracing" in cap and "rt" not in out:
            out["rt"] = _parse_table(t["rows"])
        elif "raster" in cap and "raster" not in out:
            out["raster"] = _parse_table(t["rows"])
        else:
            unlabelled.append(t)
    for mode in ("raster", "rt"):
        if mode not in out and unlabelled:
            out[mode] = _parse_table(unlabelled.pop(0)["rows"])
    return {m: out.get(m, []) for m in MODES}


def fetch_hierarchy(url: str = URL, *, timeout: float = 20.0) -> dict[str, list[dict]]:
    resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    data = parse_hierarchy(resp.text)
    if not any(data.values()):
        raise ValueError("no benchmark tables found on the page -- has its layout changed?")
    return data


# ---- cache ----------------------------------------------------------------


@dataclass
class Benchmarks:
    data: dict[str, list[dict]] = field(default_factory=lambda: {m: [] for m in MODES})
    fetched_at: datetime | None = None
    source_url: str = URL
    # set when the most recent fetch attempt failed (data may then be stale)
    error: str | None = None
    refreshed: bool = False


def get_gpu_benchmarks(db, *, force: bool = False, fetch=fetch_hierarchy, now: datetime | None = None) -> Benchmarks:
    """The cached hierarchy, re-fetched first if it's older than :data:`MAX_AGE`
    (or ``force``). Never raises on a fetch failure: the stale snapshot (or an
    empty result, on first run) comes back with ``error`` set instead."""
    now = now or datetime.now(timezone.utc)
    snap = db.get(BenchmarkSnapshot, SOURCE)
    fetched_at = snap.fetched_at if snap else None
    if fetched_at is not None and fetched_at.tzinfo is None:  # SQLite drops the offset
        fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    if snap and not force and now - fetched_at < MAX_AGE:
        return Benchmarks(data=snap.data, fetched_at=fetched_at, source_url=snap.url)

    try:
        data = fetch()
    except Exception as exc:  # noqa: BLE001 -- network, HTTP status, or layout change
        log.warning("gpu hierarchy fetch failed", extra={"error": str(exc)})
        msg = f"{type(exc).__name__}: {exc}"
        if snap:
            return Benchmarks(data=snap.data, fetched_at=fetched_at, source_url=snap.url, error=msg)
        return Benchmarks(error=msg)

    snap = snap or BenchmarkSnapshot(source=SOURCE)
    snap.url, snap.fetched_at, snap.data = URL, now, data
    db.add(snap)
    db.flush()
    return Benchmarks(data=data, fetched_at=now, source_url=URL, refreshed=True)


# ---- matching query objects to hierarchy rows -----------------------------

_BRANDS = re.compile(r"\b(nvidia|geforce|amd|radeon|intel)\b")
_MEM_SUFFIX = re.compile(r"\s*(\d+)\s*gb$")


def normalize_gpu_name(name: str) -> str:
    """``"GeForce RTX 4060 Ti 16GB"`` -> ``"rtx 4060 ti 16gb"``. Radeon VII keeps
    its brand, since ``"vii"`` alone isn't a name anyone searches for."""
    s = (name or "").lower().strip()
    if s != "radeon vii":
        s = _BRANDS.sub(" ", s)
    s = re.sub(r"(\d+)\s+gb\b", r"\1gb", s)
    return re.sub(r"\s+", " ", s).strip()


BRANDS = {"nvidia": "Nvidia", "amd": "AMD", "intel": "Intel"}
_BRAND_PATTERNS = (
    ("nvidia", re.compile(r"\b(nvidia|geforce|rtx|gtx|titan)\b")),
    ("amd", re.compile(r"\b(amd|radeon|rx|r9|vega)\b")),
    ("intel", re.compile(r"\b(intel|arc)\b")),
)


def gpu_brand(name: str) -> str | None:
    """``"GeForce RTX 5090"`` -> ``"nvidia"``; ``"Radeon RX 6600"`` -> ``"amd"``;
    ``"Intel Arc B580"`` -> ``"intel"``; None when it can't tell."""
    s = (name or "").lower()
    for brand, pattern in _BRAND_PATTERNS:
        if pattern.search(s):
            return brand
    return None


def match_benchmark(content: str, rows: list[dict]) -> dict | None:
    """The hierarchy row for a query object's search phrase, or None.

    Exact match on the normalised name first. Failing that, a query without a
    memory size (``"RTX 4060 Ti"``) matches the hierarchy's sized variants
    (``"... Ti 8GB"`` / ``"... Ti 16GB"``), taking the smallest -- the base
    model, which is what an unqualified listing title usually is.
    """
    want = normalize_gpu_name(content)
    if not want:
        return None
    by_name = {normalize_gpu_name(r["name"]): r for r in rows}
    if want in by_name:
        return by_name[want]
    if _MEM_SUFFIX.search(want):
        return None
    sized = []
    for norm, r in by_name.items():
        m = _MEM_SUFFIX.search(norm)
        if m and norm[: m.start()] == want:
            sized.append((int(m.group(1)), r))
    return min(sized, key=lambda t: t[0])[1] if sized else None
