"""Anomaly detection: checks run against every ingested result.

Four kinds, each with its own rule (see ``DEFAULT_RULES``):

- ``outlier``: a kept listing priced far from its run's median -- usually not
  the product at all ("box only", a whole PC), and it drags the low price;
- ``move``: a run's median jumps away from the vendor's recent median by more
  than usual, or climbs/falls steadily for several runs in a row;
- ``cut``: a buy-it-now listing that got meaningfully cheaper and now sits at
  or under the product median -- a buying signal, not a data problem;
- ``health``: a run that finished without errors but kept far fewer listings
  than usual, which usually means the vendor's page changed under the scraper.

Everything is compared per vendor: vendors price the same product differently,
so another vendor's prices are never the baseline. Detection is idempotent --
each anomaly carries a ``dedupe_key`` built from its run (and listing), so an
ingest replay never raises the same thing twice.
"""

from __future__ import annotations

import copy
import statistics
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.logging import get_logger
from orchestrator.models import (
    Anomaly,
    AnomalyRuleSet,
    Listing,
    ListingPricePoint,
    PriceObservation,
    QueryObject,
    TrackedListing,
)
from orchestrator.prices import recompute_for_tracked

log = get_logger("orchestrator.anomalies")

KINDS = ("outlier", "move", "cut", "health")
SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2}

DEFAULT_RULES: dict[str, dict] = {
    # flag a listing priced below low_pct% or above high_pct% of its run's median
    "outlier": {"enabled": True, "low_pct": 45, "high_pct": 220, "auctions": "never", "action": "flag"},
    # flag a run whose median is min_pct% and swing_multiple x the usual
    # run-to-run swing away from the window_days median; or a steady move over
    # sustained_runs runs in a row totalling min_pct%
    "move": {"enabled": True, "window_days": 7, "min_pct": 10, "swing_multiple": 3,
             "sustained_runs": 3, "action": "flag"},
    # flag a buy-it-now listing that drops more than min_pct% between runs and
    # ends up within near_median_pct% of the run median, or below it
    "cut": {"enabled": True, "min_pct": 15, "near_median_pct": 10, "action": "flag"},
    # flag a run keeping fewer than kept_pct% of its recent average, or dropping
    # more than dropped_pct% of what it found
    "health": {"enabled": True, "kept_pct": 40, "dropped_pct": 80, "action": "exclude"},
}

# which values each action field may take, for validating form input
ACTIONS = {
    "outlier": ("flag", "hide"),
    "move": ("flag", "exclude"),
    "cut": ("flag", "log"),
    "health": ("exclude", "flag"),
}
AUCTION_MODES = ("never", "always")

_MIN_LISTINGS_FOR_OUTLIERS = 5  # a median of 3 listings says little about any one of them
_MIN_HISTORY = 3  # runs needed before "usual" means anything
_HEALTH_LOOKBACK = 10
_SWING_FLOOR_PCT = 0.5  # a perfectly flat history would make any wobble look extreme


# ---- rules ------------------------------------------------------------------


def _merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for kind, values in (override or {}).items():
        if kind in out and isinstance(values, dict):
            out[kind].update({k: v for k, v in values.items() if k in out[kind]})
    return out


def rules_for(db: Session, category_id: str | None = None) -> dict:
    """The rules in force for a product: code defaults, then the stored
    ``default`` overrides, then its category's overrides."""
    rules = _merge(DEFAULT_RULES, _stored(db, "default"))
    if category_id:
        rules = _merge(rules, _stored(db, category_id))
    return rules


def _stored(db: Session, scope: str) -> dict:
    row = db.get(AnomalyRuleSet, scope)
    return row.config if row else {}


def save_rules(db: Session, scope: str, rules: dict) -> None:
    """Store only what differs from what ``scope`` would otherwise inherit, so
    a later change to the defaults still reaches categories that didn't
    override that number."""
    inherited = DEFAULT_RULES if scope == "default" else rules_for(db, None)
    diff: dict = {}
    for kind, values in rules.items():
        changed = {k: v for k, v in values.items() if inherited.get(kind, {}).get(k) != v}
        if changed:
            diff[kind] = changed
    row = db.get(AnomalyRuleSet, scope)
    if row is None:
        db.add(AnomalyRuleSet(scope=scope, config=diff))
    else:
        row.config = diff
    db.flush()  # so rules_for() in the same transaction sees it (Session.get skips pending rows)


def parse_rules_form(form, base: dict) -> dict:
    """Rules from the Detection rules form, falling back to ``base`` for any
    field that's missing or not a number."""
    rules = copy.deepcopy(base)
    for kind, values in rules.items():
        # an unchecked checkbox sends nothing, so a present "<kind>.present"
        # marker tells "turned off" apart from "not on this form"
        if form.get(f"{kind}.present"):
            values["enabled"] = form.get(f"{kind}.enabled") == "on"
        for field, current in list(values.items()):
            if field in ("enabled",):
                continue
            raw = form.get(f"{kind}.{field}")
            if raw is None or raw == "":
                continue
            if field == "action":
                if raw in ACTIONS[kind]:
                    values[field] = raw
            elif field == "auctions":
                if raw in AUCTION_MODES:
                    values[field] = raw
            else:
                try:
                    num = float(raw)
                except ValueError:
                    continue
                if num >= 0:
                    values[field] = int(num) if float(num).is_integer() else num
    return rules


# ---- history helpers ----------------------------------------------------------


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _clean_history(db: Session, obs: PriceObservation) -> list[PriceObservation]:
    """Earlier trustworthy runs of this product at this vendor, oldest first."""
    return list(db.scalars(
        select(PriceObservation)
        .where(
            PriceObservation.vendor_id == obs.vendor_id,
            PriceObservation.query_object_id == obs.query_object_id,
            PriceObservation.status != "error",
            PriceObservation.excluded.is_(False),
            PriceObservation.observed_at < obs.observed_at,
        )
        .order_by(PriceObservation.observed_at.asc())
    ).all())


def _pct(new: float, base: float) -> float:
    return (new - base) / base * 100 if base else 0.0


def _money(v: float) -> str:
    return f"${v:,.0f}"


# ---- detectors ---------------------------------------------------------------


def _detect_outliers(obs: PriceObservation, listings: list[Listing], cfg: dict) -> list[dict]:
    visible = [l for l in listings if not l.hidden]
    if obs.price_median is None or len(visible) < _MIN_LISTINGS_FOR_OUTLIERS:
        return []
    median = obs.price_median
    low_line = median * cfg["low_pct"] / 100
    high_line = median * cfg["high_pct"] / 100
    prices = [l.price for l in visible]
    found = []
    for l in visible:
        if l.tracked_listing_id is None:
            continue  # no stable identity to act on or dedupe by
        auction = l.listing_type == "auction"
        if auction and cfg["auctions"] == "never":
            continue
        ratio = l.price / median if median else 1
        if l.price < low_line:
            below = round((1 - ratio) * 100)
            title = f"{'Auction' if auction else 'Listing'} priced {below}% below the median"
            severity = "low" if auction else ("high" if ratio < 0.25 else "med")
            side, line = "low", low_line
            others = [(x.price, x.listing_type) for x in visible if x is not l and x.listing_type != "auction"]
            effect = {"low_if_removed": min((p for p, _ in others), default=None)}
        elif l.price > high_line:
            title = f"{'Auction' if auction else 'Listing'} priced {ratio:.1f}× the median"
            severity = "low" if auction else ("high" if ratio > 3 else "med")
            side, line, effect = "high", high_line, {}
        else:
            continue
        found.append({
            "kind": "outlier", "severity": severity, "title": title,
            "tracked_listing_id": l.tracked_listing_id,
            "dedupe_key": f"outlier:{obs.job_id}:{l.tracked_listing_id}",
            "summary": f'"{l.title or "(untitled)"}" at {_money(l.price)} while this run\'s median is '
                       f"{_money(median)}.",
            "details": {
                "listing_title": l.title, "link": l.link, "price": l.price, "median": median,
                "listing_type": l.listing_type, "side": side, "threshold": round(line, 2),
                "rule_pct": cfg["low_pct"] if side == "low" else cfg["high_pct"],
                "run_prices": sorted(prices)[:200], **effect,
            },
        })
    return found


def _detect_move(obs: PriceObservation, history: list[PriceObservation], cfg: dict) -> list[dict]:
    if obs.price_median is None:
        return []
    series = [h for h in history if h.price_median is not None]
    if len(series) < _MIN_HISTORY:
        return []
    at = _as_utc(obs.observed_at)
    window = [h for h in series if _as_utc(h.observed_at) >= at - timedelta(days=cfg["window_days"])]
    if len(window) < _MIN_HISTORY:
        window = series[-_MIN_HISTORY * 2:]
    baseline = statistics.median(h.price_median for h in window)
    tail = series[-11:]  # the last 10 run-to-run steps
    steps = [abs(_pct(b.price_median, a.price_median)) for a, b in zip(tail, tail[1:], strict=False)]
    swing = max(statistics.mean(steps) if steps else 0.0, _SWING_FLOOR_PCT)
    move = _pct(obs.price_median, baseline)
    recent = [{"at": _as_utc(h.observed_at).isoformat(), "median": h.price_median} for h in series[-13:]]
    recent.append({"at": at.isoformat(), "median": obs.price_median})
    kept_usual = statistics.mean(h.listings_kept or 0 for h in series[-_HEALTH_LOOKBACK:])
    base_details = {
        "median": obs.price_median, "previous": series[-1].price_median, "baseline": round(baseline, 2),
        "swing_pct": round(swing, 1), "window_days": cfg["window_days"], "series": recent,
        "kept": obs.listings_kept, "kept_usual": round(kept_usual),
    }

    if abs(move) > cfg["min_pct"] and abs(move) > cfg["swing_multiple"] * swing:
        direction = "fell" if move < 0 else "rose"
        thin = obs.listings_kept is not None and kept_usual and obs.listings_kept < kept_usual * 0.6
        summary = (f"The median {direction} to {_money(obs.price_median)} against a {cfg['window_days']}-day "
                   f"median of {_money(baseline)}; runs here usually move about {swing:.1f}%.")
        if thin:
            summary += (f" Only {obs.listings_kept} listings were kept (usually about {round(kept_usual)}), "
                        "so a change in which listings showed up may explain it.")
        return [{
            "kind": "move", "severity": "high" if abs(move) > 2 * cfg["min_pct"] else "med",
            "title": f"Median {direction} {abs(move):.0f}% in one run",
            "dedupe_key": f"move:{obs.job_id}", "summary": summary,
            "details": {**base_details, "move_pct": round(move, 1), "pattern": "sudden"},
        }]

    # a steady move: flagged once, by the run that completes the streak
    n = int(cfg["sustained_runs"])
    chain = [h.price_median for h in series] + [obs.price_median]
    if n >= 2 and len(chain) > n:
        diffs = [b - a for a, b in zip(chain, chain[1:], strict=False)]
        sign = 1 if diffs[-1] > 0 else -1 if diffs[-1] < 0 else 0
        streak = 0
        for d in reversed(diffs):
            if sign and d * sign > 0:
                streak += 1
            else:
                break
        total = _pct(chain[-1], chain[-1 - streak]) if streak else 0.0
        if streak == n and abs(total) > cfg["min_pct"]:
            direction = "rose" if sign > 0 else "fell"
            return [{
                "kind": "move", "severity": "med",
                "title": f"Median {direction} {abs(total):.0f}% over {n} runs",
                "dedupe_key": f"move:{obs.job_id}",
                "summary": (f"A steady move from {_money(chain[-1 - streak])} to {_money(chain[-1])} across the "
                            f"last {n} runs, each one {'higher' if sign > 0 else 'lower'} than the one before."),
                "details": {**base_details, "move_pct": round(total, 1), "pattern": "sustained"},
            }]
    return []


def _detect_cuts(db: Session, obs: PriceObservation, listings: list[Listing], cfg: dict) -> list[dict]:
    if obs.price_median is None:
        return []
    candidates = {l.tracked_listing_id: l for l in listings
                  if l.tracked_listing_id and l.listing_type == "buy_now" and not l.hidden}
    if not candidates:
        return []
    points = db.scalars(
        select(ListingPricePoint)
        .where(ListingPricePoint.tracked_listing_id.in_(list(candidates)))
        .order_by(ListingPricePoint.observed_at.asc())
    ).all()
    by_tracked: dict[int, list[ListingPricePoint]] = {}
    for p in points:
        by_tracked.setdefault(p.tracked_listing_id, []).append(p)
    ceiling = obs.price_median * (1 + cfg["near_median_pct"] / 100)
    found = []
    for tid, l in candidates.items():
        history = by_tracked.get(tid, [])
        idx = next((i for i, p in enumerate(history) if p.job_id == obs.job_id), None)
        if not idx:  # not recorded for this run, or its first sighting
            continue
        before = history[idx - 1].price
        drop = _pct(l.price, before)
        if drop < -cfg["min_pct"] and l.price <= ceiling:
            found.append({
                "kind": "cut", "severity": "low",
                "title": f"Listing price cut {abs(drop):.0f}%",
                "tracked_listing_id": tid,
                "dedupe_key": f"cut:{obs.job_id}:{tid}",
                "summary": (f'"{l.title or "(untitled)"}" went from {_money(before)} to {_money(l.price)}; '
                            f"this run's median is {_money(obs.price_median)}."),
                "details": {
                    "listing_title": l.title, "link": l.link, "before": before, "price": l.price,
                    "drop_pct": round(drop, 1), "median": obs.price_median,
                    "series": [{"at": _as_utc(p.observed_at).isoformat(), "price": p.price}
                               for p in history[:idx + 1][-20:]],
                },
            })
    return found


def _detect_health(obs: PriceObservation, history: list[PriceObservation], cfg: dict) -> list[dict]:
    recent = history[-_HEALTH_LOOKBACK:]
    if len(recent) < _MIN_HISTORY:
        return []
    kept = obs.listings_kept or 0
    found = obs.listings_found or 0
    avg = statistics.mean(h.listings_kept or 0 for h in recent)
    dropped_share = (found - kept) / found * 100 if found else 0.0
    low_kept = avg > 0 and kept < avg * cfg["kept_pct"] / 100
    heavy_drops = found > 0 and dropped_share > cfg["dropped_pct"]
    if not (low_kept or heavy_drops):
        return []
    if low_kept:
        title = f"Only {kept} listing{'s' if kept != 1 else ''} kept, usually about {round(avg)}"
        summary = (f"The run finished without errors but kept {kept} of {found} found. The vendor's page may "
                   "have changed and broken the selectors.")
    else:
        title = f"{dropped_share:.0f}% of found listings were dropped"
        summary = (f"{found - kept} of the {found} listings found were filtered out. A search phrase or "
                   "exclude list may have stopped matching what the vendor shows.")
    return [{
        "kind": "health",
        "severity": "high" if kept == 0 or (avg and kept < avg * 0.2) else "med",
        "title": title, "dedupe_key": f"health:{obs.job_id}", "summary": summary,
        "details": {"kept": kept, "found": found, "kept_avg": round(avg, 1),
                    "dropped_pct": round(dropped_share, 1),
                    "kept_series": [h.listings_kept or 0 for h in recent] + [kept]},
    }]


def detect(db: Session, obs: PriceObservation, rules: dict) -> list[dict]:
    """Every anomaly ``obs``'s run shows under ``rules``, without saving anything."""
    if obs.status == "error":
        return []  # a failed run is already visible on Runs; there's nothing to compare
    listings = list(db.scalars(select(Listing).where(Listing.job_id == obs.job_id)).all())
    history = _clean_history(db, obs)
    found: list[dict] = []
    if rules["health"]["enabled"]:
        found += _detect_health(obs, history, rules["health"])
    if rules["outlier"]["enabled"]:
        found += _detect_outliers(obs, listings, rules["outlier"])
    if rules["move"]["enabled"]:
        found += _detect_move(obs, history, rules["move"])
    if rules["cut"]["enabled"]:
        found += _detect_cuts(db, obs, listings, rules["cut"])
    return found


# ---- persisting + acting -----------------------------------------------------


def detect_for_run(db: Session, job_id: str) -> list[Anomaly]:
    """Run every enabled check against a just-ingested run, save what's new
    and apply each rule's automatic action. Returns the newly raised anomalies."""
    obs = db.scalars(select(PriceObservation).where(PriceObservation.job_id == job_id)).first()
    if obs is None:
        return []
    q = db.get(QueryObject, obs.query_object_id)
    rules = rules_for(db, q.category_id if q else None)
    created = []
    for found in detect(db, obs, rules):
        if db.scalar(select(Anomaly.id).where(Anomaly.dedupe_key == found["dedupe_key"])):
            continue
        a = Anomaly(vendor_id=obs.vendor_id, query_object_id=obs.query_object_id, job_id=obs.job_id,
                    tracked_listing_id=found.get("tracked_listing_id"),
                    **{k: found[k] for k in ("kind", "severity", "title", "summary", "details", "dedupe_key")})
        db.add(a)
        created.append(a)
        _auto_action(db, a, obs, rules[a.kind])
    return created


def _auto_action(db: Session, a: Anomaly, obs: PriceObservation, cfg: dict) -> None:
    action = cfg.get("action")
    if a.kind == "outlier" and action == "hide":
        resolve(db, a, "hide_listing", automatic=True)
    elif a.kind in ("move", "health") and action == "exclude":
        obs.excluded = True
        a.details = {**(a.details or {}), "auto_excluded": True}
    elif a.kind == "cut" and action == "log":
        a.status, a.resolution, a.reviewed_at = "reviewed", "Logged", datetime.now(timezone.utc)


RESOLUTIONS = {
    "dismiss": "Dismissed",
    "confirm": "Confirmed real",
    "hide_listing": "Listing hidden",
    "exclude_run": "Left out of trends",
    "include_run": "Kept in trends",
    "exclude_phrase": "Exclude added",
    "rerun": "Re-run queued",
}


def resolve(db: Session, a: Anomaly, action: str, *, phrase: str = "", automatic: bool = False) -> str:
    """Apply an action to an anomaly and mark it reviewed. Returns a short
    description of what happened. The caller commits."""
    if action == "reopen":
        a.status, a.resolution, a.reviewed_at = "open", "", None
        return "Reopened"
    if action not in RESOLUTIONS:
        raise ValueError(f"unknown action {action!r}")
    obs = db.scalars(select(PriceObservation).where(PriceObservation.job_id == a.job_id)).first()
    label = RESOLUTIONS[action]

    if action == "hide_listing" and a.tracked_listing_id:
        t = db.get(TrackedListing, a.tracked_listing_id)
        if t is not None:
            t.hidden = True
            recompute_for_tracked(db, [t.id])  # every run it was in, not just this one
    elif action in ("exclude_run", "include_run") and obs is not None:
        obs.excluded = action == "exclude_run"
    elif action == "exclude_phrase":
        from orchestrator.excludes import apply_excludes_to_query

        phrase = (phrase or "").strip()
        q = db.get(QueryObject, a.query_object_id)
        if not phrase or q is None:
            raise ValueError("an exclude phrase is required")
        if phrase.lower() not in (p.lower() for p in (q.exclude or [])):
            q.exclude = [*(q.exclude or []), phrase]
        apply_excludes_to_query(db, q)  # hides matches and recomputes the runs they were in
        label = f'Excluded "{phrase}"'

    a.status = "reviewed"
    a.resolution = f"{label} (automatic)" if automatic else label
    a.reviewed_at = datetime.now(timezone.utc)
    return label


# ---- preview -------------------------------------------------------------------


def preview_counts(db: Session, rules: dict, *, days: int = 30, category_id: str | None = None) -> dict:
    """How many anomalies of each kind ``rules`` would have raised over the
    last ``days``, without saving anything -- for tuning a rule before it
    gets noisy. With ``category_id``, only that category's products."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(PriceObservation).where(PriceObservation.observed_at >= since)
    if category_id:
        qids = list(db.scalars(select(QueryObject.id).where(QueryObject.category_id == category_id)).all())
        stmt = stmt.where(PriceObservation.query_object_id.in_(qids or ["__none__"]))
    counts = {k: 0 for k in KINDS}
    for obs in db.scalars(stmt).all():
        for found in detect(db, obs, rules):
            counts[found["kind"]] += 1
    counts["days"] = days
    return counts
