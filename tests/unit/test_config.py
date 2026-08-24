"""Settings, derived URLs and the Secrets interface."""

from __future__ import annotations

import pytest

from app.core.config import EnvSecrets, Secrets, Settings, get_secrets


def test_defaults_are_local_friendly() -> None:
    s = Settings()
    assert s.environment == "local"
    assert s.app_name == "common-app-base"
    assert s.secrets_provider == "env"


def test_database_url_is_async_dsn() -> None:
    s = Settings(postgres_host="db", postgres_port=5433, postgres_user="u", postgres_db="d")
    assert s.database_url.startswith("postgresql+asyncpg://")
    assert "@db:5433/d" in s.database_url


def test_sync_database_url_is_psycopg2() -> None:
    assert Settings().sync_database_url.startswith("postgresql+psycopg2://")


def test_redis_url_includes_password_only_when_set() -> None:
    assert Settings(redis_password=None).redis_url == "redis://localhost:6379/0"
    assert Settings(redis_password="pw").redis_url == "redis://:pw@localhost:6379/0"


def test_celery_falls_back_to_redis_url() -> None:
    s = Settings()
    assert s.broker_url == s.redis_url
    assert s.result_backend == s.redis_url


def test_celery_honours_explicit_broker() -> None:
    s = Settings(celery_broker_url="redis://other:6379/2")
    assert s.broker_url == "redis://other:6379/2"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("*", ["*"]), ("a.com,b.com", ["a.com", "b.com"]), ("a.com, b.com ", ["a.com", "b.com"])],
)
def test_cors_origins_parsing(raw: str, expected: list[str]) -> None:
    assert Settings(cors_allow_origins=raw).cors_origins_list == expected


def test_env_secrets_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_SECRET", "value-123")
    secrets: Secrets = EnvSecrets()
    assert secrets.get("SOME_SECRET") == "value-123"
    assert secrets.require("SOME_SECRET") == "value-123"


def test_env_secrets_default_and_require(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEFINITELY_MISSING", raising=False)
    secrets = EnvSecrets()
    assert secrets.get("DEFINITELY_MISSING") is None
    assert secrets.get("DEFINITELY_MISSING", "fallback") == "fallback"
    with pytest.raises(KeyError):
        secrets.require("DEFINITELY_MISSING")


def test_get_secrets_returns_env_provider_by_default() -> None:
    assert isinstance(get_secrets(), EnvSecrets)


def test_invalid_environment_is_rejected() -> None:
    with pytest.raises(ValueError, match="environment"):
        Settings(environment="production")  # type: ignore[arg-type]
