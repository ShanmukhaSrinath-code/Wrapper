"""Wire shapes.

Separate from the ORM model on purpose: the model is what the database holds,
these are what the API promises. Coupling them means a column rename becomes a
breaking API change.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Priority = Literal["low", "normal", "high", "urgent"]
Status = Literal["open", "in_progress", "resolved", "closed"]


class TicketCreate(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(default="", max_length=10_000)
    priority: Priority = "normal"
    assignee: str | None = Field(default=None, max_length=256)


class TicketUpdate(BaseModel):
    """All fields optional -- this is a PATCH."""

    status: Status | None = None
    priority: Priority | None = None
    assignee: str | None = Field(default=None, max_length=256)
    resolution_note: str | None = Field(default=None, max_length=10_000)


class TicketRead(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    status: Status
    priority: Priority
    requester: str
    assignee: str | None
    attachment_name: str | None
    resolution_note: str | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TicketList(BaseModel):
    items: list[TicketRead]
    total: int
    limit: int
    offset: int


class TicketStats(BaseModel):
    """Deliberately cheap to compute and deliberately cached."""

    total: int
    by_status: dict[str, int]
    by_priority: dict[str, int]
    unassigned: int
