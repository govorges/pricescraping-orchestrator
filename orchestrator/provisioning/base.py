"""Provider-agnostic node provisioning interface.

A *node* is a scalable unit of worker capacity. On SaladCloud it is a container
group; locally it is a set of labelled Docker containers on this machine. Both
join the same Redis consumer group, so scaling a node = changing how many worker
consumers exist.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class ProvisioningError(RuntimeError):
    """Raised when a provider call fails (daemon down, API error, unknown id)."""


@dataclass
class NodeSpec:
    image: str
    replicas: int = 1
    env: dict[str, str] = field(default_factory=dict)
    cpu: int | None = None
    memory_mb: int | None = None
    gpu_class: str | None = None
    name: str | None = None


@dataclass
class NodeInfo:
    id: str
    provider: str
    status: str
    replicas_desired: int
    replicas_running: int
    image: str
    meta: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "status": self.status,
            "replicas_desired": self.replicas_desired,
            "replicas_running": self.replicas_running,
            "image": self.image,
            "meta": self.meta,
        }


class Provisioner(ABC):
    provider: str = "base"

    @abstractmethod
    def create_node(self, spec: NodeSpec) -> NodeInfo: ...

    @abstractmethod
    def list_nodes(self) -> list[NodeInfo]: ...

    @abstractmethod
    def get_node(self, node_id: str) -> NodeInfo: ...

    @abstractmethod
    def scale(self, node_id: str, replicas: int) -> NodeInfo: ...

    @abstractmethod
    def delete_node(self, node_id: str) -> None: ...

    def close(self) -> None:  # noqa: B027 - optional cleanup hook, not abstract
        pass
