from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ledger.core.models import StoredEvent

if TYPE_CHECKING:
    import asyncpg


def resolve_model_version(model_versions: dict[str, str], agent_id: str | None) -> str | None:
    """Look up model version from a model_versions dict by agent_id.

    New v2 writes key model_versions by agent_id directly.
    Upcasted v1 events are built from a dual-keyed cache (stream_id + agent_id),
    so agent_id lookups work for both. No string parsing of stream ids needed.
    """
    if not agent_id:
        return None
    return model_versions.get(agent_id)


class BaseProjection(ABC):
    """Base class for all projections."""

    @property
    @abstractmethod
    def projection_name(self) -> str:
        """Unique name of the projection for checkpointing."""
        pass

    @property
    @abstractmethod
    def subscribed_events(self) -> list[str]:
        """List of event types this projection listens to."""
        pass

    @abstractmethod
    async def handle_event(
        self,
        event: StoredEvent,
        conn: "asyncpg.Connection | None" = None,
    ) -> None:
        """Processes a single event and updates the projection state."""
        pass
