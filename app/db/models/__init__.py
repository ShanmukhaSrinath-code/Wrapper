"""Model registry.

Every model must be imported here so Alembic autogenerate and
``Base.metadata`` see it.
"""

from app.db.base import Base
from app.db.models.example import Example

__all__ = ["Base", "Example"]
