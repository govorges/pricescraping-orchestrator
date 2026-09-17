"""CRUD for panel-managed query objects (frogscraper Include/Exclude filter specs)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import Category, QueryObject
from orchestrator.schemas import QueryObject as QueryObjectSchema

bp = Blueprint("queries", __name__, url_prefix="/api/v1/query-objects")

_FIELDS = (
    "content",
    "include",
    "include_settings",
    "exclude",
    "exclude_settings",
    "minimum_value",
    "maximum_value",
)


def query_dict(q: QueryObject) -> dict:
    lo, hi = q.effective_band()
    return {
        "id": q.id,
        "content": q.content,
        "include": q.include,
        "include_settings": q.include_settings,
        "exclude": q.exclude,
        "exclude_settings": q.exclude_settings,
        "minimum_value": q.minimum_value,
        "maximum_value": q.maximum_value,
        "category_id": q.category_id,
        "effective_exclude": q.effective_exclude(),
        "effective_minimum_value": lo,
        "effective_maximum_value": hi,
        "snapshot": q.snapshot(),
        "created_at": q.created_at.isoformat() if q.created_at else None,
        "updated_at": q.updated_at.isoformat() if q.updated_at else None,
    }


def _apply(q: QueryObject, body: dict) -> None:
    for key in _FIELDS:
        if key in body:
            setattr(q, key, body[key])
    if "category_id" in body:
        q.category_id = body["category_id"] or None


def _validate(q: QueryObject) -> str | None:
    try:
        QueryObjectSchema.model_validate(q.snapshot())
    except ValidationError as exc:
        return str(exc)
    return None


@bp.get("")
def list_queries():
    db = get_db()
    rows = db.scalars(select(QueryObject).order_by(QueryObject.id)).all()
    return jsonify([query_dict(q) for q in rows])


@bp.post("")
def create_query():
    db = get_db()
    body = request.get_json(silent=True) or {}
    qid = (body.get("id") or "").strip()
    if not qid:
        return jsonify({"error": "id is required"}), 400
    if db.get(QueryObject, qid) is not None:
        return jsonify({"error": f"query object '{qid}' already exists"}), 409
    if body.get("category_id") and db.get(Category, body["category_id"]) is None:
        return jsonify({"error": f"unknown category '{body['category_id']}'"}), 422

    q = QueryObject(id=qid, content=body.get("content", ""))
    _apply(q, body)
    err = _validate(q)
    if err:
        return jsonify({"error": "invalid query object", "detail": err}), 422
    db.add(q)
    db.commit()
    return jsonify(query_dict(q)), 201


@bp.get("/<qid>")
def get_query(qid: str):
    q = get_db().get(QueryObject, qid)
    if q is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(query_dict(q))


@bp.patch("/<qid>")
@bp.put("/<qid>")
def update_query(qid: str):
    db = get_db()
    q = db.get(QueryObject, qid)
    if q is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    if body.get("category_id") and db.get(Category, body["category_id"]) is None:
        return jsonify({"error": f"unknown category '{body['category_id']}'"}), 422
    _apply(q, body)
    err = _validate(q)
    if err:
        return jsonify({"error": "invalid query object", "detail": err}), 422
    db.commit()
    return jsonify(query_dict(q))


@bp.delete("/<qid>")
def delete_query(qid: str):
    db = get_db()
    q = db.get(QueryObject, qid)
    if q is None:
        return jsonify({"error": "not found"}), 404
    db.delete(q)
    db.commit()
    return "", 204
