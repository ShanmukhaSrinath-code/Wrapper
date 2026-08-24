"""Application configuration.

Everything the app needs to run is read from the environment through
:class:`Settings`.  Secrets are read through the :class:`Secrets` *interface*
rather than directly, so swapping the local env-var provider for Azure Key
Vault later is a config change (``SECRETS_PROVIDER=azure_key_vault``) and not a
rewrite -- see ``AzureKeyVaultSecrets`` at the bottom of this module.
"""

from __future__ import annotations

import abc
import functools
import os
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated setting into a clean list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


class Settings(BaseSettings):
    """Typed view over the process environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- app -----------------------------------------------------------------
    app_name: str = "common-app-base"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    # --- database ------------------------------------------------------------
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "appuser"
    postgres_password: str = "apppassword"  # noqa: S105 - local compose default, overridden by env
    #: Least-privilege role the *application* connects as. It owns nothing and
    #: has INSERT+SELECT only on audit_log, so the app cannot rewrite, erase or
    #: unguard its own audit trail. Migrations use `postgres_user` above, which
    #: owns the schema. See app/db/README.md.
    postgres_app_user: str = "appruntime"
    postgres_app_password: str = "appruntimepassword"  # noqa: S105 - local compose default
    postgres_db: str = "appdb"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- redis ---------------------------------------------------------------
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str | None = None
    cache_ttl_seconds: int = 60

    # --- object storage ------------------------------------------------------
    storage_provider: Literal["minio"] = "minio"
    s3_endpoint_url: str = "http://localhost:9000"
    s3_public_endpoint_url: str = "http://localhost:9000"
    s3_access_key: str = "minioadmin"
    s3_secret_key: str = "minioadmin"  # noqa: S105 - local compose default, overridden by env
    s3_bucket: str = "app-files"
    s3_region: str = "us-east-1"
    s3_presign_expiry_seconds: int = 900

    # --- celery --------------------------------------------------------------
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None

    # --- plugin discovery ----------------------------------------------------
    # Where features live. Anything importable under these packages has its
    # Celery tasks registered and its SQLAlchemy models added to Base.metadata,
    # so a feature never has to be added to a list by hand.
    plugin_packages: str = "app.services"
    #: Extra packages scanned for tasks only (the base's own demo tasks).
    task_packages: str = "app.jobs.tasks"
    #: Extra packages scanned for models only (the base's own tables).
    model_packages: str = "app.db.models,app.audit.models"

    # --- observability -------------------------------------------------------
    otel_enabled: bool = True
    otel_service_name: str = "common-app-base"
    otel_exporter_otlp_endpoint: str | None = None
    metrics_enabled: bool = True
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0

    # --- security ------------------------------------------------------------
    cors_allow_origins: str = "*"
    cors_allow_credentials: bool = False
    cors_allow_methods: str = "*"
    cors_allow_headers: str = "*"
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MiB
    hsts_max_age_seconds: int = 31_536_000

    # --- secrets -------------------------------------------------------------
    secrets_provider: Literal["env", "azure_key_vault"] = "env"
    azure_key_vault_url: str | None = Field(default=None)

    # --- derived -------------------------------------------------------------
    @property
    def plugin_packages_list(self) -> list[str]:
        return _split_csv(self.plugin_packages)

    @property
    def task_packages_list(self) -> list[str]:
        return _split_csv(self.task_packages)

    @property
    def model_packages_list(self) -> list[str]:
        return _split_csv(self.model_packages)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Async DSN for the **application**, as the least-privilege role.

        Deliberately not the owner role: the app must not be able to UPDATE,
        DELETE or TRUNCATE audit_log, nor drop the triggers that stop it.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_app_user}:{self.postgres_app_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def migration_database_url(self) -> str:
        """Async DSN as the **owner** role -- used by Alembic.

        Migrations create tables and triggers, which the runtime role must not
        be able to do. Keeping the two DSNs apart is what makes the audit-log
        hardening real rather than advisory.
        """
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sync_database_url(self) -> str:
        """Sync DSN as the **owner** role -- used by Alembic migrations only."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_allow_origins.split(",") if o.strip()]

    @property
    def cors_methods_list(self) -> list[str]:
        return [m.strip() for m in self.cors_allow_methods.split(",") if m.strip()]

    @property
    def cors_headers_list(self) -> list[str]:
        return [h.strip() for h in self.cors_allow_headers.split(",") if h.strip()]


# =============================================================================
# Secrets interface -- the swap point for Azure Key Vault
# =============================================================================
class Secrets(abc.ABC):
    """Read-only key/value secret source.

    Application code depends on this interface only.  Which concrete provider
    backs it is decided by ``SECRETS_PROVIDER`` at startup.
    """

    @abc.abstractmethod
    def get(self, name: str, default: str | None = None) -> str | None:
        """Return the secret ``name``, or ``default`` when absent."""

    def require(self, name: str) -> str:
        value = self.get(name)
        if value is None:
            raise KeyError(f"Required secret {name!r} is not set")
        return value


class EnvSecrets(Secrets):
    """Local/dev provider: secrets come from the process environment."""

    def get(self, name: str, default: str | None = None) -> str | None:
        return os.environ.get(name, default)


class AzureKeyVaultSecrets(Secrets):  # pragma: no cover - adapter stub
    """SWAP POINT: Azure Key Vault provider.

    Left unimplemented on purpose so the local build carries no Azure SDK
    dependency.  To enable::

        uv add azure-identity azure-keyvault-secrets
        SECRETS_PROVIDER=azure_key_vault
        AZURE_KEY_VAULT_URL=https://<vault>.vault.azure.net/

    then fill in ``__init__``/``get`` with ``SecretClient`` +
    ``DefaultAzureCredential``.  Nothing else in the app changes.
    """

    def __init__(self, vault_url: str) -> None:
        self._vault_url = vault_url
        raise NotImplementedError(
            "AzureKeyVaultSecrets is a stub. Install azure-identity + "
            "azure-keyvault-secrets and implement get() to enable it."
        )

    def get(self, name: str, default: str | None = None) -> str | None:
        raise NotImplementedError


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


@functools.lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    """Process-wide secrets provider, chosen by ``SECRETS_PROVIDER``."""
    settings = get_settings()
    if settings.secrets_provider == "azure_key_vault":
        if not settings.azure_key_vault_url:
            raise ValueError(
                "AZURE_KEY_VAULT_URL must be set when SECRETS_PROVIDER=azure_key_vault"
            )
        return AzureKeyVaultSecrets(settings.azure_key_vault_url)
    return EnvSecrets()


settings = get_settings()


if __name__ == "__main__":  # pragma: no cover
    import json

    print(json.dumps(settings.model_dump(mode="json"), indent=2, sort_keys=True))
