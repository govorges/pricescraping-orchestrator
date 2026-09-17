from __future__ import annotations

import pytest

from orchestrator.provisioning.base import NodeSpec, ProvisioningError
from orchestrator.provisioning.local import LocalProvider


class _Image:
    def __init__(self, tag):
        self.tags = [tag] if tag else []


class _Container:
    def __init__(self, name, labels, image, status="running", environment=None):
        self.name = name
        self.labels = labels
        self.image = _Image(image)
        self.status = status
        self.removed = False
        self.environment = environment or {}

    def remove(self, force=False):  # noqa: ARG002
        self.removed = True
        self._store.remove(self)


class _Containers:
    def __init__(self):
        self._list: list[_Container] = []

    def run(self, image, *, detach, name, environment, network, labels, restart_policy):  # noqa: ARG002
        c = _Container(name, dict(labels), image, environment=dict(environment or {}))
        c._store = self._list
        self._list.append(c)
        return c

    def list(self, *, all=False, filters=None):  # noqa: A002, ARG002
        filters = filters or {}
        wanted = filters.get("label", [])
        if isinstance(wanted, str):
            wanted = [wanted]
        out = []
        for c in self._list:
            ok = True
            for w in wanted:
                if "=" in w:
                    k, v = w.split("=", 1)
                    if c.labels.get(k) != v:
                        ok = False
                elif w not in c.labels:
                    ok = False
            if ok:
                out.append(c)
        return out


class _Docker:
    def __init__(self):
        self.containers = _Containers()


@pytest.fixture
def provider(settings):
    return LocalProvider(settings, client=_Docker())


def test_create_and_list_node(provider):
    """Creating a node with N replicas starts N containers, and list_nodes reports the same node back."""
    info = provider.create_node(NodeSpec(image="pricescraping-worker:latest", replicas=2, name="n1"))
    assert info.id == "n1"
    assert info.replicas_running == 2

    nodes = provider.list_nodes()
    assert len(nodes) == 1
    assert nodes[0].id == "n1"
    assert nodes[0].replicas_running == 2


def test_scale_up_and_down(provider):
    """Scaling a node up starts new containers, and scaling it back down removes the excess."""
    provider.create_node(NodeSpec(image="img", replicas=1, name="n1"))
    up = provider.scale("n1", 3)
    assert up.replicas_running == 3
    down = provider.scale("n1", 1)
    assert down.replicas_running == 1


def test_delete_node(provider):
    """Deleting a node removes all of its containers -- list_nodes shows nothing left behind."""
    provider.create_node(NodeSpec(image="img", replicas=2, name="n1"))
    provider.delete_node("n1")
    assert provider.list_nodes() == []


def test_unknown_node_raises(provider):
    """Looking up a node id that was never created raises rather than returning a blank/default node."""
    with pytest.raises(ProvisioningError):
        provider.get_node("missing")


def test_worker_containers_get_node_name_env(provider):
    """NODE_NAME must match the orchestrator Node id so a worker's self-reported
    scrape-result 'worker.node' can be attributed back to it (jobs / min)."""
    provider.create_node(NodeSpec(image="img", replicas=2, name="n1"))
    containers = provider._containers_for("n1")
    assert len(containers) == 2
    assert all(c.environment.get("NODE_NAME") == "n1" for c in containers)
