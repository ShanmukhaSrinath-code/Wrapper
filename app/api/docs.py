"""Swagger UI and ReDoc, served under a strict Content-Security-Policy.

FastAPI's built-in docs routes emit an **inline** bootstrap script. Under the
API's `script-src 'self' https://cdn.jsdelivr.net` that script is blocked and
the page renders empty.

The lazy fix is `'unsafe-inline'`, which re-opens the exact hole CSP exists to
close. Instead these routes render the same HTML and then pin the inline script
by its SHA-256 hash, computed from the bytes actually being sent. That keeps
the policy strict *and* self-maintaining: upgrade FastAPI, the script changes,
the hash follows automatically.
"""

from __future__ import annotations

import base64
import hashlib
import re

from fastapi import APIRouter
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import HTMLResponse

from app.config import settings

router = APIRouter(include_in_schema=False)

_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.DOTALL)

#: Origins the docs pages legitimately load assets from.
_CDN = "https://cdn.jsdelivr.net"


def _script_hashes(html: bytes) -> list[str]:
    """CSP `sha256-...` sources for every inline <script> in ``html``."""
    hashes = []
    for match in _INLINE_SCRIPT.finditer(html):
        body = match.group(1)
        if body.strip():
            digest = hashlib.sha256(body).digest()
            hashes.append(f"'sha256-{base64.b64encode(digest).decode()}'")
    return hashes


def _docs_csp(html: bytes, *, allow_worker_blob: bool = False) -> str:
    """Build a CSP that permits exactly the inline scripts in this page."""
    script_src = " ".join(["'self'", _CDN, *_script_hashes(html)])
    directives = [
        "default-src 'self'",
        f"script-src {script_src}",
        # Swagger UI and ReDoc both style elements inline; there is no
        # equivalent hash mechanism that covers style attributes.
        f"style-src 'self' 'unsafe-inline' {_CDN}",
        f"img-src 'self' data: https://fastapi.tiangolo.com {_CDN}",
        f"font-src 'self' data: {_CDN}",
        "connect-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'none'",
        "form-action 'none'",
    ]
    if allow_worker_blob:
        # ReDoc renders in a web worker created from a blob URL.
        directives.append("worker-src 'self' blob:")
        directives.append("child-src 'self' blob:")
    return "; ".join(directives)


def _render(html: str, *, allow_worker_blob: bool = False) -> HTMLResponse:
    body = html.encode("utf-8")
    return HTMLResponse(
        content=body,
        headers={
            "Content-Security-Policy": _docs_csp(body, allow_worker_blob=allow_worker_blob),
            # Docs are static per build; let the browser keep them briefly.
            "Cache-Control": "no-cache",
        },
    )


@router.get("/docs")
async def swagger_ui() -> HTMLResponse:
    html = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{settings.app_name} — API",
        oauth2_redirect_url="/docs/oauth2-redirect",
    )
    return _render(html.body.decode("utf-8"))


@router.get("/docs/oauth2-redirect")
async def swagger_ui_redirect() -> HTMLResponse:
    html = get_swagger_ui_oauth2_redirect_html()
    return _render(html.body.decode("utf-8"))


@router.get("/redoc")
async def redoc() -> HTMLResponse:
    html = get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{settings.app_name} — API",
        with_google_fonts=False,
    )
    return _render(html.body.decode("utf-8"), allow_worker_blob=True)
