"""Assert the project virtualenv is real and usable.

`make install` used to report success over a broken venv. A partially-failed
`uv sync` (antivirus holding a file open, a full disk, an interrupted download)
could leave `.venv` without a `pyvenv.cfg`. From then on:

* every later `uv sync` printed "Checked N packages" and exited 0;
* but `uv run` silently fell back to the *uv-managed* interpreter, because a
  directory without `pyvenv.cfg` is not a virtualenv;
* so `make migrate` failed with `ModuleNotFoundError: No module named 'alembic'`
  while `.venv/Lib/site-packages/alembic` sat there in plain sight.

The install step was lying. This script makes it tell the truth: it runs *inside*
`uv run`, so it verifies the interpreter that real commands will actually use.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

#: Imported to prove the environment can actually run the application, not just
#: that files were copied. Covers each dependency group `make install` installs.
REQUIRED_IMPORTS = (
    "alembic",
    "app.core.config",
    "celery",
    "fastapi",
    "httpx",
    "pytest",
    "redis",
    "sqlalchemy",
    "structlog",
    "uvicorn",
)


def _fail(message: str) -> None:
    print(
        f"\n  make install did not produce a working environment.\n\n  {message}\n",
        file=sys.stderr,
    )
    print(
        "  Fix: rm -rf .venv && make install\n"
        "  (on Windows, close anything holding .venv open first -- editors, "
        "stray pytest.exe processes, antivirus scans.)\n",
        file=sys.stderr,
    )
    raise SystemExit(1)


def main() -> None:
    executable = Path(sys.executable).resolve()
    project_venv = (Path(__file__).resolve().parent.parent / ".venv").resolve()

    # 1. The marker that makes a directory a virtualenv at all. Its absence is
    #    the exact corruption that used to go unnoticed.
    if not (project_venv / "pyvenv.cfg").is_file():
        _fail(f"{project_venv / 'pyvenv.cfg'} is missing, so .venv is not a usable virtualenv.")

    # 2. `uv run` must be using *that* venv, not a managed interpreter elsewhere.
    try:
        executable.relative_to(project_venv)
    except ValueError:
        _fail(
            f"uv run is using {executable},\n  which is not inside {project_venv}.\n"
            "  Commands would run against the wrong interpreter."
        )

    # 3. The environment must actually be able to import the app and its tools.
    missing: list[str] = []
    for name in REQUIRED_IMPORTS:
        try:
            importlib.import_module(name)
        except Exception as exc:
            missing.append(f"{name} ({type(exc).__name__}: {exc})")
    if missing:
        _fail("these imports failed:\n    - " + "\n    - ".join(missing))

    print(f"venv OK: {executable}")
    print(f"         {len(REQUIRED_IMPORTS)} key imports resolve")


if __name__ == "__main__":
    main()
