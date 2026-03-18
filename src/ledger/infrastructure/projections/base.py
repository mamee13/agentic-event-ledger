from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from ledger.core.models import StoredEvent

if TYPE_CHECKING:
    import asyncpg


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
