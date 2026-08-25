"""Dev credentials must not survive outside `local`."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


# --------------------------------------------------------------------------
# Dev credentials must not survive outside `local`
# --------------------------------------------------------------------------
def test_local_still_boots_on_the_compose_defaults() -> None:
    """The whole point of the defaults: `make up` works with no .env."""
    assert Settings(environment="local").postgres_password == "apppassword"


@pytest.mark.parametrize("environment", ["dev", "staging", "prod"])
def test_deployed_environments_reject_dev_defaults(environment: str) -> None:
    with pytest.raises(ValidationError) as exc:
        Settings(environment=environment)
    message = str(exc.value)
    assert "postgres_password" in message
    assert "s3_secret_key" in message


def test_deployed_environment_boots_once_secrets_are_supplied() -> None:
    config = Settings(
        environment="prod",
        postgres_password="a-real-password",
        postgres_app_password="another-real-password",
        s3_access_key="a-real-key",
        s3_secret_key="a-real-secret",
    )
    assert config.environment == "prod"


def test_the_guard_is_derived_not_a_hand_maintained_list() -> None:
    """Every secret-looking field is checked, so a new one is covered for free."""
    from app.core.config import dev_default_secret_fields

    checked = dev_default_secret_fields()
    assert {"postgres_password", "postgres_app_password", "s3_access_key", "s3_secret_key"} <= set(
        checked
    )
    # A URL is not a secret, even though its name contains "key".
    assert "azure_key_vault_url" not in checked
