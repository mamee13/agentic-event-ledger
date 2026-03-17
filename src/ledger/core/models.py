from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field


class BaseEvent(BaseModel):
    """Base class for all domain events."""

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str
    event_version: int = 1
    payload: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class StoredEvent(BaseEvent):
    """Event as stored in and retrieved from the database."""

    stream_id: str
    stream_position: int
    global_position: int
    recorded_at: datetime


class StreamMetadata(BaseModel):
    """Metadata for an event stream."""

    model_config = ConfigDict(from_attributes=True)

    stream_id: str
    aggregate_type: str
    current_version: int = 0
    created_at: datetime
    archived_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
