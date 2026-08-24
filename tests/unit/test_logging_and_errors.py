"""Correlation context, the error schema and the security-header helper."""

from __future__ import annotations

import pytest
from starlette.datastructures import MutableHeaders

from app.core.config import Settings
from app.core.errors import (
    AppError,
    ConflictError,
    ErrorResponse,
    NotFoundError,
    PayloadTooLargeError,
    PermissionDeniedError,
)
from app.core.logging import (
    bind_request_context,
    clear_request_context,
    configure_logging,
    current_request_id,
    current_trace_id,
)
from app.core.middleware.correlation import _is_valid_request_id
from app.core.middleware.security import (
    API_CSP,
    BASE_SECURITY_HEADERS,
    apply_security_headers,
    build_cors_kwargs,
)


@pytest.fixture(autouse=True)
def _clean_context() -> None:
    clear_request_context()


def test_bind_and_read_request_context() -> None:
    bind_request_context(request_id="req-1", trace_id="t" * 32, span_id="s" * 16)
    assert current_request_id() == "req-1"
    assert current_trace_id() == "t" * 32


def test_clear_removes_context() -> None:
    bind_request_context(request_id="req-1")
    clear_request_context()
    assert current_request_id() is None


def test_context_is_empty_by_default() -> None:
    assert current_request_id() is None
    assert current_trace_id() is None


def test_configure_logging_is_idempotent() -> None:
    configure_logging()
    configure_logging()  # must not raise or duplicate handlers


# --------------------------------------------------------------------------
# inbound request-id validation
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "value",
    [
        "0f8be724-1ae5-4c1f-9819-2599be773062",
        "my-own-id-12345",
        "abc_DEF-123",
    ],
)
def test_valid_inbound_request_ids_are_accepted(value: str) -> None:
    assert _is_valid_request_id(value) is True


@pytest.mark.parametrize(
    "value",
    [
        "",  # empty
        "short",  # under 8 chars
        "x" * 129,  # over 128 chars
        "bad;id$(injection)",  # shell-ish metacharacters
        'has "quotes"',  # log-injection risk
        "new\nline",  # log forging
    ],
)
def test_hostile_inbound_request_ids_are_rejected(value: str) -> None:
    assert _is_valid_request_id(value) is False


# --------------------------------------------------------------------------
# error schema
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("exc_class", "status_code", "code"),
    [
        (NotFoundError, 404, "not_found"),
        (ConflictError, 409, "conflict"),
        (PermissionDeniedError, 403, "permission_denied"),
        (PayloadTooLargeError, 413, "payload_too_large"),
    ],
)
def test_app_error_subclasses_carry_status_and_code(
    exc_class: type[AppError], status_code: int, code: str
) -> None:
    exc = exc_class("boom")
    assert exc.status_code == status_code
    assert exc.error_code == code
    assert exc.message == "boom"


def test_error_response_omits_none_fields() -> None:
    payload = ErrorResponse(error="not_found", message="nope").model_dump(exclude_none=True)
    assert payload == {"error": "not_found", "message": "nope"}


def test_error_response_keeps_correlation_ids() -> None:
    payload = ErrorResponse(
        error="internal_error", message="m", request_id="r-1", trace_id="t-1"
    ).model_dump(exclude_none=True)
    assert payload["request_id"] == "r-1"
    assert payload["trace_id"] == "t-1"


# --------------------------------------------------------------------------
# security headers
# --------------------------------------------------------------------------
def test_apply_security_headers_sets_the_baseline() -> None:
    headers = MutableHeaders()
    apply_security_headers(headers, is_https=False)
    for name in BASE_SECURITY_HEADERS:
        assert name.lower() in headers
    assert headers["content-security-policy"] == API_CSP


def test_hsts_only_over_https() -> None:
    plain = MutableHeaders()
    apply_security_headers(plain, is_https=False)
    assert "strict-transport-security" not in plain

    secure = MutableHeaders()
    apply_security_headers(secure, is_https=True, hsts_max_age=1234)
    assert secure["strict-transport-security"] == "max-age=1234; includeSubDomains"


def test_existing_headers_are_not_overwritten() -> None:
    headers = MutableHeaders()
    headers["Cache-Control"] = "public, max-age=60"
    apply_security_headers(headers, is_https=False)
    assert headers["cache-control"] == "public, max-age=60"


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
def test_cors_exposes_correlation_headers() -> None:
    kwargs = build_cors_kwargs(Settings())
    assert "X-Request-ID" in kwargs["expose_headers"]  # type: ignore[operator]
    assert "X-Trace-ID" in kwargs["expose_headers"]  # type: ignore[operator]


def test_wildcard_origin_with_credentials_is_rejected() -> None:
    """Browsers reject this combination; fail loudly rather than silently."""
    with pytest.raises(ValueError, match="CORS_ALLOW_CREDENTIALS"):
        build_cors_kwargs(Settings(cors_allow_origins="*", cors_allow_credentials=True))


def test_explicit_origins_with_credentials_is_allowed() -> None:
    kwargs = build_cors_kwargs(
        Settings(cors_allow_origins="https://app.example.com", cors_allow_credentials=True)
    )
    assert kwargs["allow_origins"] == ["https://app.example.com"]
    assert kwargs["allow_credentials"] is True
