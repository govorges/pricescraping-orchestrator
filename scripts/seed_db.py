"""Load vendors / query objects / schedules from a JSON file into the DB.

Usage:
    python scripts/seed_db.py [config/seed.example.json]

Idempotent: existing rows (by primary key) are updated in place. Run
``alembic upgrade head`` (or ``python -m orchestrator --create-all``) first.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from orchestrator.db import session_scope
from orchestrator.models import Category, QueryObject, Schedule, Vendor
from orchestrator.schemas import QueryObject as QuerySchema
from orchestrator.schemas import VendorConfig


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("config/seed.example.json")
    data = json.loads(path.read_text(encoding="utf-8"))

    with session_scope() as db:
        for identifier, config in (data.get("vendors") or {}).items():
            VendorConfig.model_validate({"identifier": identifier, **config})
            v = db.get(Vendor, identifier) or Vendor(identifier=identifier)
            v.config = config
            v.name = config.get("metadata", {}).get("name", identifier)
            v.enabled = True
            db.add(v)
            print(f"vendor  {identifier}")

        for cid, spec in (data.get("categories") or {}).items():
            c = db.get(Category, cid) or Category(id=cid)
            c.name = spec.get("name", "")
            c.extra_exclude = spec.get("extra_exclude", [])
            c.exclude_settings = spec.get("exclude_settings", {})
            c.minimum_value_floor = spec.get("minimum_value_floor")
            c.maximum_value_ceiling = spec.get("maximum_value_ceiling")
            c.notes = spec.get("notes", "")
            db.add(c)
            print(f"category {cid}")

        for qid, spec in (data.get("query_objects") or {}).items():
            q = db.get(QueryObject, qid) or QueryObject(id=qid)
            q.content = spec.get("content", "")
            q.include = spec.get("include", [])
            q.include_settings = spec.get("include_settings", {})
            q.exclude = spec.get("exclude", [])
            q.exclude_settings = spec.get("exclude_settings", {})
            q.minimum_value = spec.get("minimum_value", 0.0)
            q.maximum_value = spec.get("maximum_value")
            q.category_id = spec.get("category_id")
            db.add(q)
            QuerySchema.model_validate(q.snapshot())
            print(f"query   {qid}")

        existing = {(s.vendor_id, s.query_object_id, s.cron) for s in db.query(Schedule).all()}
        for spec in data.get("schedules") or []:
            key = (spec["vendor_id"], spec["query_object_id"], spec["cron"])
            if key in existing:
                continue
            db.add(
                Schedule(
                    name=spec.get("name", ""),
                    vendor_id=spec["vendor_id"],
                    query_object_id=spec["query_object_id"],
                    cron=spec["cron"],
                    options=spec.get("options", {}),
                    enabled=spec.get("enabled", True),
                )
            )
            print(f"sched   {spec.get('name') or key}")

    print("done")


if __name__ == "__main__":
    main()
