"""CRUD for query-object categories (scoped global exclude phrases + price guard rails)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import Category

bp = Blueprint("categories", __name__, url_prefix="/api/v1/categories")

_FIELDS = ("name", "extra_exclude", "exclude_settings", "minimum_value_floor",
          "maximum_value_ceiling", "notes")


def category_dict(c: Category) -> dict:
    return {
        "id": c.id,
        "name": c.name,
        "extra_exclude": c.extra_exclude,
        "exclude_settings": c.exclude_settings,
        "minimum_value_floor": c.minimum_value_floor,
        "maximum_value_ceiling": c.maximum_value_ceiling,
        "notes": c.notes,
        "query_count": len(c.queries),
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


def _apply(c: Category, body: dict) -> None:
    for key in _FIELDS:
        if key in body:
            setattr(c, key, body[key])


@bp.get("")
def list_categories():
    rows = get_db().scalars(select(Category).order_by(Category.id)).all()
    return jsonify([category_dict(c) for c in rows])


@bp.post("")
def create_category():
    db = get_db()
    body = request.get_json(silent=True) or {}
    cid = (body.get("id") or "").strip()
    if not cid:
        return jsonify({"error": "id is required"}), 400
    if db.get(Category, cid):
        return jsonify({"error": f"category '{cid}' already exists"}), 409
    c = Category(id=cid)
    _apply(c, body)
    db.add(c)
    db.commit()
    return jsonify(category_dict(c)), 201


@bp.get("/<cid>")
def get_category(cid: str):
    c = get_db().get(Category, cid)
    if c is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(category_dict(c))


@bp.patch("/<cid>")
@bp.put("/<cid>")
def update_category(cid: str):
    db = get_db()
    c = db.get(Category, cid)
    if c is None:
        return jsonify({"error": "not found"}), 404
    _apply(c, request.get_json(silent=True) or {})
    db.commit()
    return jsonify(category_dict(c))


@bp.delete("/<cid>")
def delete_category(cid: str):
    db = get_db()
    c = db.get(Category, cid)
    if c is None:
        return jsonify({"error": "not found"}), 404
    db.delete(c)
    db.commit()
    return "", 204
