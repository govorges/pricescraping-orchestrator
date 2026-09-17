"""CRUD for panel-managed vendor configs (frogscraper-compatible blocks)."""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import Vendor
from orchestrator.schemas import VendorConfig

bp = Blueprint("vendors", __name__, url_prefix="/api/v1/vendors")


def vendor_dict(v: Vendor) -> dict:
    return {
        "identifier": v.identifier,
        "name": v.name,
        "enabled": v.enabled,
        "config": v.config,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "updated_at": v.updated_at.isoformat() if v.updated_at else None,
    }


def _validate_config(identifier: str, config: dict) -> tuple[dict | None, str | None]:
    try:
        VendorConfig.model_validate({"identifier": identifier, **(config or {})})
    except ValidationError as exc:
        return None, str(exc)
    return config or {}, None


@bp.get("")
def list_vendors():
    db = get_db()
    rows = db.scalars(select(Vendor).order_by(Vendor.identifier)).all()
    return jsonify([vendor_dict(v) for v in rows])


@bp.post("")
def create_vendor():
    db = get_db()
    body = request.get_json(silent=True) or {}
    identifier = (body.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"error": "identifier is required"}), 400
    if db.get(Vendor, identifier) is not None:
        return jsonify({"error": f"vendor '{identifier}' already exists"}), 409

    config, err = _validate_config(identifier, body.get("config") or {})
    if err:
        return jsonify({"error": "invalid vendor config", "detail": err}), 422

    v = Vendor(
        identifier=identifier,
        name=body.get("name") or config.get("metadata", {}).get("name", identifier),
        enabled=bool(body.get("enabled", True)),
        config=config,
    )
    db.add(v)
    db.commit()
    return jsonify(vendor_dict(v)), 201


@bp.get("/<identifier>")
def get_vendor(identifier: str):
    v = get_db().get(Vendor, identifier)
    if v is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(vendor_dict(v))


@bp.patch("/<identifier>")
@bp.put("/<identifier>")
def update_vendor(identifier: str):
    db = get_db()
    v = db.get(Vendor, identifier)
    if v is None:
        return jsonify({"error": "not found"}), 404
    body = request.get_json(silent=True) or {}
    if "config" in body:
        config, err = _validate_config(identifier, body["config"] or {})
        if err:
            return jsonify({"error": "invalid vendor config", "detail": err}), 422
        v.config = config
    if "name" in body:
        v.name = body["name"]
    if "enabled" in body:
        v.enabled = bool(body["enabled"])
    db.commit()
    return jsonify(vendor_dict(v))


@bp.delete("/<identifier>")
def delete_vendor(identifier: str):
    db = get_db()
    v = db.get(Vendor, identifier)
    if v is None:
        return jsonify({"error": "not found"}), 404
    db.delete(v)
    db.commit()
    return "", 204
