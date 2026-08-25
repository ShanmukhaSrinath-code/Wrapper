"""CORS denies by default; the wildcard is an explicit opt-in."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import Settings
from app.core.middleware.security import build_cors_kwargs


def _app(config: Settings) -> FastAPI:
    app = FastAPI()
    app.add_middleware(CORSMiddleware, **build_cors_kwargs(config))

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def _origin_allowed(config: Settings, origin: str) -> bool:
    """Whether a browser at ``origin`` would be permitted to read the response."""
    transport = httpx.ASGITransport(app=_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get("/ping", headers={"Origin": origin})
    return "access-control-allow-origin" in response.headers


def test_cors_denies_by_default() -> None:
    # `_env_file=None`: this asserts the *shipped default*, not whatever the
    # developer running the suite happens to have in their local .env.
    config = Settings(_env_file=None)
    assert config.cors_origins_list == []
    assert build_cors_kwargs(config)["allow_origins"] == []


def test_the_shipped_env_example_does_not_enable_wildcard_cors() -> None:
    """The file people copy to .env must not hand them an open API."""
    lines = Path(".env.example").read_text(encoding="utf-8").splitlines()
    active = [ln for ln in lines if ln.startswith("CORS_ALLOW_ORIGINS=")]
    assert active == ["CORS_ALLOW_ORIGINS="]


def test_wildcard_cors_is_an_explicit_opt_in() -> None:
    assert build_cors_kwargs(Settings(cors_allow_origins="*"))["allow_origins"] == ["*"]


def test_named_origins_still_work() -> None:
    config = Settings(cors_allow_origins="https://app.example.com,https://admin.example.com")
    assert build_cors_kwargs(config)["allow_origins"] == [
        "https://app.example.com",
        "https://admin.example.com",
    ]


# --------------------------------------------------------------------------
# ...and the default actually blocks a browser, not just the settings object
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_default_config_blocks_a_cross_origin_browser() -> None:
    assert not await _origin_allowed(Settings(_env_file=None), "https://evil.example.com")


@pytest.mark.asyncio
async def test_explicit_wildcard_allows_it() -> None:
    config = Settings(_env_file=None, cors_allow_origins="*")
    assert await _origin_allowed(config, "https://evil.example.com")


@pytest.mark.asyncio
async def test_named_origin_is_allowed_and_others_are_not() -> None:
    config = Settings(_env_file=None, cors_allow_origins="https://app.example.com")
    assert await _origin_allowed(config, "https://app.example.com")
    assert not await _origin_allowed(config, "https://evil.example.com")
