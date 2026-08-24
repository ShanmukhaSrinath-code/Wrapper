"""Playwright end-to-end tests.

These drive a real browser, which is the only way to check things an HTTP
client cannot: that the OpenAPI UI actually renders, that Swagger's
"Try it out" reaches the live API, and -- importantly -- that the strict CSP
from Phase 11 does not break the docs page.
"""

from __future__ import annotations

import re

import pytest
from playwright.async_api import ConsoleMessage, Page, expect

pytestmark = pytest.mark.e2e


async def test_swagger_ui_renders(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/docs", wait_until="networkidle")

    await expect(page).to_have_title(re.compile("common-app-base", re.I))
    await expect(page.locator("#swagger-ui")).to_be_visible()
    # The spec loaded and produced operations -- an empty shell would not.
    await expect(page.locator(".opblock").first).to_be_visible(timeout=20_000)


async def test_docs_page_is_not_broken_by_the_csp(page: Page, base_url: str) -> None:
    """The API CSP is `default-src 'none'`; /docs pins its inline script by hash.

    If that regressed, Swagger's bootstrap script would be blocked and the page
    would render empty with CSP violations in the console.
    """
    violations: list[str] = []

    def record(msg: ConsoleMessage) -> None:
        if "Content Security Policy" in msg.text:
            violations.append(msg.text)

    page.on("console", record)
    await page.goto(f"{base_url}/docs", wait_until="networkidle")
    await expect(page.locator("#swagger-ui .opblock").first).to_be_visible(timeout=20_000)
    assert not violations, f"CSP blocked the docs page: {violations}"


async def test_expected_endpoints_are_documented(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/docs", wait_until="networkidle")
    await page.wait_for_selector(".opblock", timeout=20_000)

    body = await page.locator("body").inner_text()
    for path in ("/health/live", "/health/ready", "/files", "/demo/cached", "/demo/job"):
        assert path in body, f"{path} is missing from the OpenAPI docs"


async def test_redoc_renders(page: Page, base_url: str) -> None:
    await page.goto(f"{base_url}/redoc", wait_until="networkidle")
    await expect(page.locator("h1").first).to_be_visible(timeout=30_000)


async def test_try_it_out_calls_the_live_api(page: Page, base_url: str) -> None:
    """Drive Swagger's own UI to execute a request against the running service."""
    await page.goto(f"{base_url}/docs", wait_until="networkidle")
    await page.wait_for_selector(".opblock", timeout=20_000)

    # Swagger derives the element id from the tag plus the operationId.
    operation = page.locator('[id^="operations-health-live_health_live_get"]')
    await operation.click()
    await operation.get_by_role("button", name=re.compile("Try it out", re.I)).click()
    await operation.get_by_role("button", name=re.compile("Execute", re.I)).click()

    responses = operation.locator(".responses-wrapper")
    await expect(responses).to_contain_text("200", timeout=30_000)
    await expect(responses).to_contain_text('"status": "ok"')


async def test_browser_receives_the_correlation_headers(page: Page, base_url: str) -> None:
    """The browser must be able to read X-Request-ID (CORS expose_headers)."""
    async with page.expect_response(lambda r: r.url.endswith("/health/live")) as info:
        await page.goto(f"{base_url}/health/live")
    response = await info.value

    assert response.status == 200
    headers = {k.lower(): v for k, v in response.headers.items()}
    assert headers.get("x-request-id")
    assert headers.get("x-content-type-options") == "nosniff"
    assert "server" not in headers
