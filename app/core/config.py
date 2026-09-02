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

from pydantic import Field, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated setting into a clean list."""
    return [item.strip() for item in raw.split(",") if item.strip()]


#: A field is treated as a credential if its name says so. Deriving the set this
#: way -- rather than listing fields -- means a secret added later is guarded the
#: day it is added, with no list to remember.
_SECRET_NAME_PARTS = ("password", "secret", "token", "_key")
#: ...except locators, which merely *name* a secret store.
_SECRET_NAME_SUFFIXES = ("_url", "_endpoint", "_provider", "_name")


def _looks_like_a_secret(field_name: str) -> bool:
    if field_name.endswith(_SECRET_NAME_SUFFIXES):
        return False
    return any(part in field_name for part in _SECRET_NAME_PARTS)


def dev_default_secret_fields() -> list[str]:
    """Fields the deployed-environment guard checks.

    Every credential-looking field that ships with a **non-empty default** --
    a default is only ever a local convenience, so carrying one into a deployed
    environment means the real secret was never supplied.
    """
    return sorted(
        name
        for name, field in Settings.model_fields.items()
        if _looks_like_a_secret(name) and isinstance(field.default, str) and field.default
    )


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
    #: How many times a task retries a *transient* failure before giving up.
    #: Capped deliberately: an uncapped retry turns one poisoned message into a
    #: permanent load source that outlives the incident that caused it.
    task_max_retries: int = 3
    #: Ceiling on the exponential backoff between retries, in seconds.
    task_retry_backoff_max_seconds: int = 60

    # --- plugin discovery ----------------------------------------------------
    # Where features live. Anything importable under these packages has its
    # Celery tasks registered and its SQLAlchemy models added to Base.metadata,
    # so a feature never has to be added to a list by hand.
    plugin_packages: str = "app.services"
    #: Extra packages scanned for tasks only. Empty by default: the base ships
    #: no tasks outside the plugin seam, so PLUGIN_PACKAGES covers everything.
    task_packages: str = ""
    #: Extra packages scanned for models only -- the base's own tables, which
    #: live in core rather than in the plugin seam.
    model_packages: str = "app.core.db.models,app.core.audit.models"

    # --- observability -------------------------------------------------------
    otel_enabled: bool = True
    otel_service_name: str = "common-app-base"
    otel_exporter_otlp_endpoint: str | None = None
    metrics_enabled: bool = True
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0

    # --- security ------------------------------------------------------------
    #: Deny by default. A browser origin that may call this API is a deployment
    #: decision, so the wildcard is an explicit opt-in rather than what you get
    #: by forgetting to set it. Server-to-server callers are unaffected: CORS is
    #: a browser mechanism.
    cors_allow_origins: str = ""
    cors_allow_credentials: bool = False
    cors_allow_methods: str = "*"
    cors_allow_headers: str = "*"
    max_request_body_bytes: int = 10 * 1024 * 1024  # 10 MiB
    hsts_max_age_seconds: int = 31_536_000

    # --- rate limiting -------------------------------------------------------
    #: On by default. A limit that ships off is a limit nobody turns on until
    #: after the first incident.
    rate_limit_enabled: bool = True
    #: Requests allowed per client per window. Generous on purpose: this is an
    #: abuse and runaway-retry brake, not a quota product.
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60
    #: Paths never counted. Probes and scrapes fire constantly and throttling a
    #: liveness probe would make the orchestrator restart a healthy process.
    rate_limit_exempt_paths: str = "/health/live,/health/ready,/metrics"
    #: Whether `X-Forwarded-For` may identify the client. Default **off**: when
    #: nothing strips the header, any caller can forge it and get a fresh budget
    #: per request. Turn it on only behind a proxy you control.
    rate_limit_trust_forwarded_for: bool = False

    # --- outbound http -------------------------------------------------------
    #: Timeouts are not optional. An unbounded outbound call turns a slow
    #: dependency into exhausted workers here, which is how one team's incident
    #: becomes everyone's.
    http_connect_timeout_seconds: float = 5.0
    http_read_timeout_seconds: float = 15.0
    http_max_connections: int = 100
    #: Retries beyond the first attempt, for transient failures only.
    http_max_retries: int = 2
    http_retry_backoff_seconds: float = 0.2
    #: Consecutive failures to one host before the breaker opens.
    http_breaker_enabled: bool = True
    http_breaker_failure_threshold: int = 5
    http_breaker_reset_seconds: float = 30.0

    # --- secrets -------------------------------------------------------------
    secrets_provider: Literal["env", "azure_key_vault"] = "env"
    azure_key_vault_url: str | None = Field(default=None)

    # --- deployment safety ---------------------------------------------------
    @model_validator(mode="after")
    def _reject_dev_defaults_outside_local(self) -> Settings:
        """Refuse to start a deployed environment on the local-compose secrets.

        These defaults exist so `make up` works from a clean clone. Booting
        `prod` on them is not a smaller problem than crashing -- it is a
        published database password -- so this fails loudly at construction,
        before anything binds a port.
        """
        if self.environment == "local":
            return self

        offenders = [
            name
            for name in dev_default_secret_fields()
            if getattr(self, name) == Settings.model_fields[name].default
        ]
        if offenders:
            raise ValueError(
                f"ENVIRONMENT={self.environment!r} is still using the local development "
                f"defaults for: {', '.join(offenders)}. Set each one from your secret "
                f"store (see SECRETS_PROVIDER) before deploying."
            )
        return self

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

    @property
    def rate_limit_exempt_paths_set(self) -> frozenset[str]:
        return frozenset(_split_csv(self.rate_limit_exempt_paths))


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
