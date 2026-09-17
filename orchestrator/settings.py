"""Environment-driven configuration for the orchestrator process.

Mirrors the style of ``pricescraping-worker``'s ``worker/settings.py``: a single
pydantic-settings model, parsed once via :func:`get_settings`.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ---- Persistence ----
    database_url: str = "sqlite:///./_local/orchestrator.db"

    # ---- Queue (must line up with the worker) ----
    redis_url: str = "redis://localhost:6379/0"
    job_stream: str = "scrape.jobs"
    consumer_group: str = "workers"
    deadletter_stream: str = "scrape.jobs.deadletter"

    # ---- Result ingest ----
    ingest_token: str = "dev-token"
    ingest_path: str = "/api/v1/ingest/scrape-results"

    # ---- Scheduler ----
    scheduler_enabled: bool = True
    reconcile_interval_s: int = 30

    # ---- Autoscaling ----
    autoscale_enabled: bool = False
    autoscale_per_replica_target: int = 10
    autoscale_min_replicas: int = 0
    autoscale_max_replicas: int = 10

    # ---- Node provisioning ----
    provider: str = "local"  # local | salad
    worker_image: str = "pricescraping-worker:latest"
    worker_network: str = "pricescraping_default"
    worker_env_redis_url: str = "redis://redis:6379/0"
    worker_env_orchestrator_base_url: str = "http://orchestrator:8000"

    # ---- SaladCloud ----
    salad_base_url: str = "https://api.salad.com/api/public"
    salad_api_key: str = ""
    salad_org: str = ""
    salad_project: str = ""
    salad_container_group_prefix: str = "pricescraping-worker"

    # ---- Process ----
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    secret_key: str = Field(default="dev-secret")

    def worker_base_env(self) -> dict[str, str]:
        """Environment every orchestrator-started worker node inherits."""
        return {
            "REDIS_URL": self.worker_env_redis_url,
            "JOB_STREAM": self.job_stream,
            "CONSUMER_GROUP": self.consumer_group,
            "DEADLETTER_STREAM": self.deadletter_stream,
            "ORCHESTRATOR_BASE_URL": self.worker_env_orchestrator_base_url,
            "ORCHESTRATOR_TOKEN": self.ingest_token,
            "INGEST_PATH": self.ingest_path,
        }


_settings: Settings | None = None


def get_settings() -> Settings:
    """Process-wide singleton so the environment is parsed once."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings_cache() -> None:
    """Test hook: drop the cached singleton so the next call re-reads the env."""
    global _settings
    _settings = None
