"""Regression tests for Fix 6 -- `make install` must not lie.

A partially-failed `uv sync` could leave `.venv` without a `pyvenv.cfg`. Every
later `uv sync` then printed "Checked N packages" and exited 0, while `uv run`
silently used a different interpreter -- so `make migrate` died with
`ModuleNotFoundError: No module named 'alembic'` even though the package was on
disk.

These tests run the verifier in a subprocess against a *fabricated* environment,
so they exercise the real script rather than a reimplementation of it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "scripts" / "verify_venv.py"


def test_verifier_passes_against_the_real_venv() -> None:
    """The healthy case: this suite is running inside a working venv."""
    result = subprocess.run(
        [sys.executable, str(VERIFIER)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "venv OK" in result.stdout


def test_verifier_fails_when_pyvenv_cfg_is_missing(tmp_path: Path) -> None:
    """The exact corruption the audit hit, reproduced in a scratch tree.

    A copy of the script sits beside a `.venv` directory that has no
    `pyvenv.cfg`. The script derives the expected venv from its own location, so
    this is the same check `make install` performs.
    """
    fake_repo = tmp_path / "repo"
    (fake_repo / "scripts").mkdir(parents=True)
    (fake_repo / ".venv").mkdir()  # a directory, but not a virtualenv
    script_copy = fake_repo / "scripts" / "verify_venv.py"
    script_copy.write_text(VERIFIER.read_text(encoding="utf-8"), encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(script_copy)],
        capture_output=True,
        text=True,
        check=False,
        cwd=fake_repo,
    )

    assert result.returncode == 1, "a broken venv was reported as a successful install"
    assert "pyvenv.cfg is missing" in result.stderr
    assert "rm -rf .venv" in result.stderr, "the error must say how to fix it"


def test_verifier_lists_the_imports_it_requires() -> None:
    """The check must cover the app itself, not just third-party packages."""
    from scripts.verify_venv import REQUIRED_IMPORTS

    assert "app.config" in REQUIRED_IMPORTS
    assert "alembic" in REQUIRED_IMPORTS, "alembic is what actually broke"


@pytest.mark.parametrize("name", ["celery", "fastapi", "sqlalchemy", "uvicorn"])
def test_required_imports_cover_the_runtime_stack(name: str) -> None:
    from scripts.verify_venv import REQUIRED_IMPORTS

    assert name in REQUIRED_IMPORTS
