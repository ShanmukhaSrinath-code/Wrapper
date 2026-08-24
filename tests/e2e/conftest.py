"""Playwright fixtures, async flavour.

pytest-playwright ships *sync* fixtures, which cannot run inside the event loop
that `asyncio_mode = auto` creates for the rest of the suite -- mixing them
raises `RuntimeError: Runner.run() cannot be called from a running event loop`
as soon as `make test` runs unit and e2e tests in one process. Driving
`playwright.async_api` directly keeps the whole suite asyncio-native.

The fixtures are deliberately **function-scoped**. A session-scoped browser is
faster, but Playwright's driver subprocess is bound to the loop that created
it, and pytest-asyncio gives each test its own loop -- the mismatch deadlocks
rather than failing, which is the worst way for a test suite to break.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Page, async_playwright


@pytest.fixture
async def page() -> AsyncIterator[Page]:
    """A headless Chromium page, isolated per test."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(ignore_https_errors=True)
        page = await context.new_page()
        try:
            yield page
        finally:
            await context.close()
            await browser.close()
