"""LocalProvider — worker capacity as labelled Docker containers on this host.

Talks to the local Docker daemon via the ``docker`` SDK. Every container it
starts carries two labels:

* ``orchestrator.managed=true``
* ``orchestrator.node_id=<node id>``

so ``list_nodes`` / ``scale`` / ``delete_node`` can find and count them without
any external bookkeeping. Scaling a node to N means "make the number of running
containers with that ``node_id`` equal to N".
"""

from __future__ import annotations

import uuid

from orchestrator.logging import get_logger
from orchestrator.provisioning.base import NodeInfo, NodeSpec, Provisioner, ProvisioningError
from orchestrator.settings import Settings, get_settings

log = get_logger("orchestrator.provisioning.local")

_MANAGED = "orchestrator.managed"
_NODE_ID = "orchestrator.node_id"


class LocalProvider(Provisioner):
    provider = "local"

    def __init__(self, settings: Settings | None = None, *, client=None) -> None:
        self._settings = settings or get_settings()
        self._client = client
        if self._client is None:
            try:
                import docker

                self._client = docker.from_env()
            except Exception as exc:  # noqa: BLE001
                raise ProvisioningError(
                    f"cannot reach the Docker daemon: {exc}. Is Docker Desktop running?"
                ) from exc

    # ---- helpers ----

    def _containers_for(self, node_id: str):
        containers = self._client.containers.list(
            all=True, filters={"label": [f"{_MANAGED}=true", f"{_NODE_ID}={node_id}"]}
        )
        # deterministic order so scale-down removes the highest-indexed replicas
        return sorted(containers, key=lambda c: c.name)

    def _all_managed(self):
        return self._client.containers.list(all=True, filters={"label": f"{_MANAGED}=true"})

    def _env_for(self, spec: NodeSpec, node_id: str) -> dict[str, str]:
        # NODE_NAME lets a worker report which orchestrator Node it belongs to
        # in its scrape results (see ScrapeResult.worker.node) -- without this
        # the worker falls back to its container hostname, which never matches
        # a Node.id and makes any per-node "jobs / min" figure impossible.
        return {**self._settings.worker_base_env(), "NODE_NAME": node_id, **spec.env}

    def _network_arg(self) -> str | None:
        """Return the configured network if it exists, else None (default bridge)."""
        net = self._settings.worker_network
        if not net:
            return None
        try:
            if self._client.networks.list(names=[net]):
                return net
        except Exception:  # noqa: BLE001
            return net
        log.warning("network %r not found; starting worker on the default bridge", net)
        return None

    def _remove_quietly(self, name: str) -> None:
        try:
            self._client.containers.get(name).remove(force=True)
        except Exception:  # noqa: BLE001
            pass

    def _require_image(self, image: str) -> None:
        """Fail fast with a build hint instead of attempting a registry pull."""
        try:
            self._client.images.get(image)
        except AttributeError:
            return  # test fake without an images API
        except Exception as exc:  # noqa: BLE001 - docker.errors.ImageNotFound / APIError
            raise ProvisioningError(
                f"worker image {image!r} is not present locally. Build it first:\n"
                f"    docker build -t {image} ../pricescraping-worker\n"
                f"(original error: {exc})"
            ) from exc

    def _run_one(self, node_id: str, spec: NodeSpec, index: int) -> None:
        name = f"{self._settings.salad_container_group_prefix}-{node_id}-{index}"
        log.info("starting worker container %s", name)
        try:
            self._client.containers.run(
                spec.image,
                detach=True,
                name=name,
                environment=self._env_for(spec, node_id),
                network=self._network_arg(),
                labels={_MANAGED: "true", _NODE_ID: node_id},
                restart_policy={"Name": "unless-stopped"},
            )
        except Exception as exc:  # noqa: BLE001 - clean up a partially-created container
            self._remove_quietly(name)
            raise ProvisioningError(f"could not start worker container {name}: {exc}") from exc

    @staticmethod
    def _node_info(node_id: str, containers, image: str, desired: int | None = None) -> NodeInfo:
        running = [c for c in containers if getattr(c, "status", "") == "running"]
        img = image or (
            containers[0].image.tags[0] if containers and containers[0].image.tags else ""
        )
        return NodeInfo(
            id=node_id,
            provider="local",
            status="running" if running else ("stopped" if containers else "empty"),
            replicas_desired=len(containers) if desired is None else desired,
            replicas_running=len(running),
            image=img,
            meta={"containers": [c.name for c in containers]},
        )

    # ---- Provisioner API ----

    def create_node(self, spec: NodeSpec) -> NodeInfo:
        node_id = spec.name or f"local-{uuid.uuid4().hex[:8]}"
        if self._containers_for(node_id):
            raise ProvisioningError(f"node '{node_id}' already exists")
        self._require_image(spec.image)
        for i in range(max(spec.replicas, 0)):
            self._run_one(node_id, spec, i)
        return self._node_info(node_id, self._containers_for(node_id), spec.image, spec.replicas)

    def list_nodes(self) -> list[NodeInfo]:
        by_node: dict[str, list] = {}
        for c in self._all_managed():
            nid = c.labels.get(_NODE_ID, "unknown")
            by_node.setdefault(nid, []).append(c)
        return [self._node_info(nid, cs, "") for nid, cs in sorted(by_node.items())]

    def get_node(self, node_id: str) -> NodeInfo:
        containers = self._containers_for(node_id)
        if not containers:
            raise ProvisioningError(f"unknown node '{node_id}'")
        return self._node_info(node_id, containers, "")

    def scale(self, node_id: str, replicas: int) -> NodeInfo:
        replicas = max(int(replicas), 0)
        containers = self._containers_for(node_id)
        if not containers and replicas == 0:
            raise ProvisioningError(f"unknown node '{node_id}'")

        image = (
            containers[0].image.tags[0]
            if containers and containers[0].image.tags
            else self._settings.worker_image
        )
        spec = NodeSpec(image=image, replicas=replicas)

        current = len(containers)
        if replicas > current:
            used = {
                int(c.name.rsplit("-", 1)[-1])
                for c in containers
                if c.name.rsplit("-", 1)[-1].isdigit()
            }
            idx = 0
            for _ in range(replicas - current):
                while idx in used:
                    idx += 1
                self._run_one(node_id, spec, idx)
                used.add(idx)
        elif replicas < current:
            for c in containers[replicas:]:
                log.info("removing worker container %s", c.name)
                c.remove(force=True)

        return self._node_info(node_id, self._containers_for(node_id), image, replicas)

    def delete_node(self, node_id: str) -> None:
        containers = self._containers_for(node_id)
        if not containers:
            raise ProvisioningError(f"unknown node '{node_id}'")
        for c in containers:
            c.remove(force=True)
