"""Node lifecycle API — list (reconciled), provision, scale, delete."""

from __future__ import annotations

from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import select

from orchestrator import get_db
from orchestrator.models import Node
from orchestrator.provisioning import NodeSpec, ProvisioningError, get_provisioner

bp = Blueprint("nodes", __name__, url_prefix="/api/v1/nodes")


def node_dict(n: Node) -> dict:
    return {
        "id": n.id,
        "provider": n.provider,
        "status": n.status,
        "replicas_desired": n.replicas_desired,
        "replicas_running": n.replicas_running,
        "image": n.image,
        "meta": n.meta,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "last_seen_at": n.last_seen_at.isoformat() if n.last_seen_at else None,
    }


def _settings():
    return current_app.config["SETTINGS"]


def _refresh_from_provider(db) -> None:
    """Best-effort: pull live node state into the DB before listing."""
    try:
        provisioner = get_provisioner(_settings())
    except ProvisioningError:
        return
    try:
        infos = provisioner.list_nodes()
    except ProvisioningError:
        return
    finally:
        provisioner.close()

    now = datetime.now(timezone.utc)
    seen = set()
    for info in infos:
        seen.add(info.id)
        row = db.get(Node, info.id) or Node(id=info.id, created_at=now)
        row.provider = info.provider
        row.status = info.status
        row.replicas_desired = info.replicas_desired
        row.replicas_running = info.replicas_running
        row.image = info.image or row.image
        row.meta = info.meta
        row.last_seen_at = now
        db.add(row)
    for row in db.scalars(select(Node)).all():
        if row.id not in seen and row.provider == _settings().provider:
            row.status = "gone"
            row.replicas_running = 0
    db.commit()


@bp.get("")
def list_nodes():
    db = get_db()
    if request.args.get("refresh", "1") != "0":
        _refresh_from_provider(db)
    rows = db.scalars(select(Node).order_by(Node.id)).all()
    return jsonify([node_dict(n) for n in rows])


@bp.post("")
def create_node():
    body = request.get_json(silent=True) or {}
    settings = _settings()
    spec = NodeSpec(
        image=body.get("image") or settings.worker_image,
        replicas=int(body.get("replicas", 1)),
        env=body.get("env") or {},
        cpu=body.get("cpu"),
        memory_mb=body.get("memory_mb"),
        gpu_class=body.get("gpu_class"),
        name=body.get("name"),
    )
    try:
        provisioner = get_provisioner(settings)
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 503
    try:
        info = provisioner.create_node(spec)
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        provisioner.close()

    db = get_db()
    now = datetime.now(timezone.utc)
    row = db.get(Node, info.id) or Node(id=info.id, created_at=now)
    row.provider = info.provider
    row.status = info.status
    row.replicas_desired = info.replicas_desired
    row.replicas_running = info.replicas_running
    row.image = info.image
    row.meta = info.meta
    row.last_seen_at = now
    db.add(row)
    db.commit()
    return jsonify(node_dict(row)), 201


@bp.post("/<node_id>/scale")
def scale_node(node_id: str):
    body = request.get_json(silent=True) or {}
    if "replicas" not in body:
        return jsonify({"error": "replicas is required"}), 400
    try:
        provisioner = get_provisioner(_settings())
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 503
    try:
        info = provisioner.scale(node_id, int(body["replicas"]))
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        provisioner.close()

    db = get_db()
    row = db.get(Node, node_id)
    if row is not None:
        row.status = info.status
        row.replicas_desired = info.replicas_desired
        row.replicas_running = info.replicas_running
        row.last_seen_at = datetime.now(timezone.utc)
        db.commit()
    return jsonify(info.as_dict())


@bp.delete("/<node_id>")
def delete_node(node_id: str):
    try:
        provisioner = get_provisioner(_settings())
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 503
    try:
        provisioner.delete_node(node_id)
    except ProvisioningError as exc:
        return jsonify({"error": str(exc)}), 502
    finally:
        provisioner.close()

    db = get_db()
    row = db.get(Node, node_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return "", 204
