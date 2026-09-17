"""SQLite round-trips ``DateTime(timezone=True)`` values as naive datetimes, which
would otherwise serialise as an offset-less ISO string. Browsers treat a
timezone-less ISO datetime string as *local* time (`Date.parse`), which would
skew every price-over-time chart by the viewer's UTC offset. Everything this
app stores is UTC, so stamp the offset back on before serialising.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
