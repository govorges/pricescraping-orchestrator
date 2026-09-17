"""SaladProvider — worker capacity as a SaladCloud container group.

REST API: https://docs.salad.com/reference/saladcloud-api
Base URL ``https://api.salad.com/api/public``; auth header ``Salad-Api-Key``.

Container groups live under an organization + project:

    POST   /organizations/{org}/projects/{project}/container-groups
    GET    /organizations/{org}/projects/{project}/container-groups
    GET    /organizations/{org}/projects/{project}/container-groups/{name}
    PATCH  /organizations/{org}/projects/{project}/container-groups/{name}
    DELETE /organizations/{org}/projects/{project}/container-groups/{name}
    POST   /organizations/{org}/projects/{project}/container-groups/{name}/start
    POST   /organizations/{org}/projects/{project}/container-groups/{name}/stop

The request/response mapping is deliberately isolated in :meth:`_spec_to_body`
and :meth:`_to_nodeinfo` so a schema change is a one-spot fix.
"""

from __future__ import annotations

import re
import uuid

import httpx

from orchestrator.logging import get_logger
from orchestrator.provisioning.base import NodeInfo, NodeSpec, Provisioner, ProvisioningError
from orchestrator.settings import Settings, get_settings

log = get_logger("orchestrator.provisioning.salad")

_NAME_RE = re.compile(r"[^a-z0-9-]")


def _slug(value: str) -> str:
    value = _NAME_RE.sub("-", value.lower()).strip("-")
    return re.sub(r"-+", "-", value)[:63] or "worker"


class SaladProvider(Provisioner):
    provider = "salad"

    def __init__(self, settings: Settings | None = None, *, client: httpx.Client | None = None):
        self._settings = settings or get_settings()
        if not (self._settings.salad_api_key and self._settings.salad_org
                and self._settings.salad_project):
            raise ProvisioningError(
                "SaladCloud provider needs SALAD_API_KEY, SALAD_ORG and SALAD_PROJECT"
            )
        self._client = client or httpx.Client(
            base_url=self._settings.salad_base_url.rstrip("/"),
            headers={
                "Salad-Api-Key": self._settings.salad_api_key,
                "Content-Type": "application/json",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    # ---- request/response mapping (keep API-shape churn here) ----

    @property
    def _base(self) -> str:
        s = self._settings
        return f"/organizations/{s.salad_org}/projects/{s.salad_project}/container-groups"

    def _spec_to_body(self, name: str, spec: NodeSpec) -> dict:
        resources: dict = {
            "cpu": spec.cpu or 1,
            "memory": spec.memory_mb or 2048,
        }
        if spec.gpu_class:
            resources["gpu_classes"] = [spec.gpu_class]
        # NODE_NAME lets a worker report which orchestrator Node it belongs to
        # in its scrape results -- without it "jobs / min" per node is impossible.
        env = {**self._settings.worker_base_env(), "NODE_NAME": name, **spec.env}
        return {
            "name": name,
            "container": {
                "image": spec.image,
                "resources": resources,
                "environment_variables": env,
            },
            "replicas": max(spec.replicas, 1),
            "autostart_policy": True,
            "restart_policy": "always",
        }

    def _to_nodeinfo(self, body: dict) -> NodeInfo:
        current = body.get("current_state") or {}
        status = current.get("status") or body.get("status") or "unknown"
        running = (
            current.get("instance_status_counts", {}).get("running_count")
            if isinstance(current.get("instance_status_counts"), dict)
            else current.get("running")
        )
        replicas = body.get("replicas", 0)
        container = body.get("container") or {}
        return NodeInfo(
            id=body.get("name", ""),
            provider="salad",
            status=str(status),
            replicas_desired=int(replicas or 0),
            replicas_running=int(running or 0),
            image=container.get("image", ""),
            meta={"id": body.get("id"), "current_state": current},
        )

    def _request(self, method: str, path: str, **kw) -> dict:
        try:
            resp = self._client.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise ProvisioningError(f"SaladCloud request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ProvisioningError(
                f"SaladCloud {method} {path} -> HTTP {resp.status_code} {resp.text[:300]}"
            )
        if not resp.content:
            return {}
        return resp.json()

    # ---- Provisioner API ----

    def create_node(self, spec: NodeSpec) -> NodeInfo:
        name = _slug(spec.name or f"{self._settings.salad_container_group_prefix}-{uuid.uuid4().hex[:8]}")
        body = self._request("POST", self._base, json=self._spec_to_body(name, spec))
        log.info("created SaladCloud container group %s", name)
        return self._to_nodeinfo(body or {"name": name, "replicas": spec.replicas,
                                          "container": {"image": spec.image}})

    def list_nodes(self) -> list[NodeInfo]:
        body = self._request("GET", self._base)
        items = body.get("items", body if isinstance(body, list) else [])
        return [self._to_nodeinfo(x) for x in items]

    def get_node(self, node_id: str) -> NodeInfo:
        return self._to_nodeinfo(self._request("GET", f"{self._base}/{node_id}"))

    def scale(self, node_id: str, replicas: int) -> NodeInfo:
        body = self._request(
            "PATCH",
            f"{self._base}/{node_id}",
            json={"replicas": max(int(replicas), 0)},
            headers={"Content-Type": "application/merge-patch+json"},
        )
        return self._to_nodeinfo(body or {"name": node_id, "replicas": replicas})

    def delete_node(self, node_id: str) -> None:
        self._request("DELETE", f"{self._base}/{node_id}")

    def start(self, node_id: str) -> None:
        self._request("POST", f"{self._base}/{node_id}/start")

    def stop(self, node_id: str) -> None:
        self._request("POST", f"{self._base}/{node_id}/stop")
