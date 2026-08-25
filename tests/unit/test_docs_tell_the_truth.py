"""No comment or doc may still instruct editing the base to add a feature.

A stale instruction is worse than no instruction: it sends a developer to edit
a protected module that discovery already handles, which is exactly the failure
the plugin seam exists to prevent.
"""

from __future__ import annotations

import re
from pathlib import Path

#: "add a feature" instructions that name main.py as something you edit.
STALE = re.compile(
    r"(include|register|add|mount)[^.\n]{0,40}router[^.\n]{0,40}(in|to)\s+``?app/main\.py",
    re.IGNORECASE,
)

SEARCHED = ["app", "docs", "README.md", "ARCHITECTURE.md"]


def _candidate_files() -> list[Path]:
    files: list[Path] = []
    for target in SEARCHED:
        path = Path(target)
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            files.extend(p for p in path.rglob("*") if p.suffix in {".py", ".md"})
    return files


def test_no_doc_tells_you_to_edit_main_py() -> None:
    offenders = [
        f"{path}:{n}: {line.strip()}"
        for path in _candidate_files()
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if STALE.search(line)
    ]
    assert not offenders, "Stale 'edit main.py' instructions:\n" + "\n".join(offenders)


def test_the_plugin_seam_docstring_says_routers_are_discovered() -> None:
    import app.services

    assert app.services.__doc__ is not None
    assert "discover" in app.services.__doc__.lower()
    assert "router" in app.services.__doc__.lower()
