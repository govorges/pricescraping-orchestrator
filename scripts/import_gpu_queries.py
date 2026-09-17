"""Import frogscraper's GPU query list into the ``gpu`` category.

Source: https://github.com/govorges/frogscraper/blob/main/Query/lists/GPUs.json
(a snapshot is kept at ``config/gpus.frogscraper.json``).

Usage:
    python scripts/import_gpu_queries.py [config/gpus.frogscraper.json]

Idempotent: query object ids are derived from the search phrase
(``"RTX 5090"`` -> ``gpu-rtx-5090``), so re-running updates existing rows in
place instead of duplicating them. The category itself is not created here —
run ``alembic upgrade head`` / seed it first if ``gpu`` doesn't exist yet.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from orchestrator.db import session_scope
from orchestrator.models import Category, QueryObject
from orchestrator.schemas import QueryObject as QuerySchema

CATEGORY_ID = "gpu"


def slugify(content: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", content.lower()).strip("-")
    return f"{CATEGORY_ID}-{s}"


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/gpus.frogscraper.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    with session_scope() as db:
        if db.get(Category, CATEGORY_ID) is None:
            print(f"category '{CATEGORY_ID}' not found — create it first (e.g. in the panel)")
            sys.exit(1)

        seen: set[str] = set()
        created = updated = skipped = 0
        for spec in data.get("queries", []):
            content = (spec.get("Content") or "").strip()
            if not content:
                continue
            qid = slugify(content)
            if qid in seen:
                skipped += 1
                continue
            seen.add(qid)

            q = db.get(QueryObject, qid)
            is_new = q is None
            q = q or QueryObject(id=qid)
            q.content = content
            q.include = spec.get("Include") or []
            q.include_settings = spec.get("IncludeSettings") or {}
            q.exclude = spec.get("Exclude") or []
            q.exclude_settings = spec.get("ExcludeSettings") or {}
            q.minimum_value = spec.get("MinimumValue") or 0.0
            q.maximum_value = spec.get("MaximumValue")
            q.category_id = CATEGORY_ID
            db.add(q)
            QuerySchema.model_validate(q.snapshot())

            created += is_new
            updated += not is_new
            print(f"{'created' if is_new else 'updated'}  {qid}  ({content})")

        print(f"\n{created} created, {updated} updated, {skipped} duplicate(s) in the source skipped")


if __name__ == "__main__":
    main()
