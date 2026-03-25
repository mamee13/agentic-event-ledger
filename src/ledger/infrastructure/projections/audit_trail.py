import json
import logging

import asyncpg

from ledger.core.models import StoredEvent
from ledger.infrastructure.projections.base import BaseProjection
from ledger.schema.events import EVENT_REGISTRY

logger = logging.getLogger(__name__)


class AuditTrailProjection(BaseProjection):
    """Projection that materializes audit-stream events into a query table."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def projection_name(self) -> str:
        return "AuditTrail"

    @property
    def subscribed_events(self) -> list[str]:
        # Audit stream may contain any domain event type.
        return list(EVENT_REGISTRY.keys())

    async def handle_event(
        self, event: StoredEvent, conn: asyncpg.Connection | None = None
    ) -> None:
        if not event.stream_id.startswith("audit-"):
            return

        entity_type, entity_id = self._parse_audit_stream(event.stream_id)
        if entity_type != "loan":
            # Keep non-loan audit streams out of the application audit trail view.
            return

        source_stream_id = event.metadata.get("source_stream_id", event.stream_id)

        if conn:
            await self._handle_event_with_conn(event, entity_id, source_stream_id, conn)
        else:
            async with self._pool.acquire() as c:
                await self._handle_event_with_conn(event, entity_id, source_stream_id, c)

    async def _handle_event_with_conn(
        self,
        event: StoredEvent,
        application_id: str,
        source_stream_id: str,
        conn: asyncpg.Connection,
    ) -> None:
        await conn.execute(
            """
            INSERT INTO projection_audit_trail
            (application_id, event_type, payload, global_position, recorded_at, source_stream_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (application_id, global_position) DO NOTHING
            """,
            application_id,
            event.event_type,
            json.dumps(event.payload),
            event.global_position,
            event.recorded_at,
            source_stream_id,
        )

    def _parse_audit_stream(self, stream_id: str) -> tuple[str, str]:
        # stream_id format: audit-{entity_type}-{entity_id}
        raw = stream_id.removeprefix("audit-")
        if "-" not in raw:
            return "unknown", raw
        entity_type, entity_id = raw.split("-", 1)
        return entity_type, entity_id
