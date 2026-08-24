"""Regression tests for Fix 2 -- models must be discovered, and drops must be loud.

The bug these pin down: ``migrations/env.py`` built ``target_metadata`` from a
hand-maintained ``app/db/models/__init__.py``. A model missing from that file
still worked at runtime, so nothing warned anyone -- but the next
``alembic revision --autogenerate`` compared an incomplete metadata against the
live database and proposed ``op.drop_table()`` for the missing model's table.

Two independent defences are tested, because either alone is a single point of
failure:

1. discovery makes ``Base.metadata`` complete without anyone editing a registry;
2. a destructive-operation guard rejects ``drop_table``/``drop_column`` coming
   out of autogenerate unless ``ALLOW_DESTRUCTIVE=1`` is set explicitly.
"""

from __future__ import annotations

import pathlib
import shutil
from collections.abc import Iterator

import pytest

FEATURE_DIR = pathlib.Path(__file__).resolve().parents[2] / "app" / "services" / "_probe_feature"

MODEL_SOURCE = '''\
"""Throwaway model used by the model-discovery regression test."""

from sqlalchemy.orm import Mapped, mapped_column

from app.core.db.base import Base, UUIDPrimaryKeyMixin


class ProbeThing(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "probe_thing"

    label: Mapped[str] = mapped_column()
'''


@pytest.fixture
def feature_package() -> Iterator[str]:
    """A feature package with a model in it -- and no registry edit anywhere."""
    FEATURE_DIR.mkdir(parents=True, exist_ok=True)
    (FEATURE_DIR / "__init__.py").write_text("", encoding="utf-8")
    (FEATURE_DIR / "models.py").write_text(MODEL_SOURCE, encoding="utf-8")
    try:
        yield "app.services._probe_feature.models"
    finally:
        shutil.rmtree(FEATURE_DIR, ignore_errors=True)


def test_model_in_a_feature_package_is_discovered(feature_package: str) -> None:
    """No __init__.py edit is needed for Alembic to see a new model."""
    from app.core import discovery

    found = discovery.discover_model_modules()
    assert feature_package in found, f"{feature_package} not discovered; found={sorted(found)}"


def test_discovered_model_reaches_base_metadata(feature_package: str) -> None:
    """Discovery imports the module, so the table is in Base.metadata."""
    from app.core.db.base import Base
    from app.core.discovery import import_discovered_models

    import_discovered_models()
    assert "probe_thing" in Base.metadata.tables


def test_autogenerate_guard_blocks_a_drop_table() -> None:
    """A drop escaping autogenerate is refused unless explicitly allowed."""
    from alembic.operations import ops

    from app.core.db.migration_guard import DestructiveMigrationError, reject_destructive_ops

    directive = ops.MigrationScript(
        rev_id="probe",
        upgrade_ops=ops.UpgradeOps(ops=[ops.DropTableOp("probe_thing")]),
        downgrade_ops=ops.DowngradeOps(ops=[]),
    )

    with pytest.raises(DestructiveMigrationError) as excinfo:
        reject_destructive_ops(None, None, [directive], allow_destructive=False)

    assert "probe_thing" in str(excinfo.value)
    assert "ALLOW_DESTRUCTIVE" in str(excinfo.value)


def test_autogenerate_guard_blocks_a_drop_column() -> None:
    """Dropping a column loses data just as surely as dropping a table."""
    from alembic.operations import ops

    from app.core.db.migration_guard import DestructiveMigrationError, reject_destructive_ops

    directive = ops.MigrationScript(
        rev_id="probe",
        upgrade_ops=ops.UpgradeOps(ops=[ops.DropColumnOp("probe_thing", "label")]),
        downgrade_ops=ops.DowngradeOps(ops=[]),
    )

    with pytest.raises(DestructiveMigrationError):
        reject_destructive_ops(None, None, [directive], allow_destructive=False)


def test_autogenerate_guard_allows_drops_when_explicitly_enabled() -> None:
    """The guard is a speed bump, not a wall -- a real drop stays possible."""
    from alembic.operations import ops

    from app.core.db.migration_guard import reject_destructive_ops

    directive = ops.MigrationScript(
        rev_id="probe",
        upgrade_ops=ops.UpgradeOps(ops=[ops.DropTableOp("probe_thing")]),
        downgrade_ops=ops.DowngradeOps(ops=[]),
    )

    reject_destructive_ops(None, None, [directive], allow_destructive=True)


def test_autogenerate_guard_ignores_additive_changes() -> None:
    """Creating tables must never be blocked."""
    import sqlalchemy as sa
    from alembic.operations import ops

    from app.core.db.migration_guard import reject_destructive_ops

    directive = ops.MigrationScript(
        rev_id="probe",
        upgrade_ops=ops.UpgradeOps(
            ops=[ops.CreateTableOp("probe_thing", [sa.Column("id", sa.Integer())])]
        ),
        downgrade_ops=ops.DowngradeOps(ops=[]),
    )

    reject_destructive_ops(None, None, [directive], allow_destructive=False)
