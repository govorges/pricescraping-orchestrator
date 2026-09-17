"""Node provisioning: pick a provider from settings."""

from __future__ import annotations

from orchestrator.provisioning.base import (
    NodeInfo,
    NodeSpec,
    Provisioner,
    ProvisioningError,
)
from orchestrator.settings import Settings, get_settings

__all__ = [
    "NodeInfo",
    "NodeSpec",
    "Provisioner",
    "ProvisioningError",
    "get_provisioner",
]


def get_provisioner(settings: Settings | None = None) -> Provisioner:
    settings = settings or get_settings()
    if settings.provider == "salad":
        from orchestrator.provisioning.salad import SaladProvider

        return SaladProvider(settings)
    from orchestrator.provisioning.local import LocalProvider

    return LocalProvider(settings)
