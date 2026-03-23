import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

from ledger.core.errors import OptimisticConcurrencyError
from ledger.core.models import BaseEvent, StoredEvent, StreamMetadata
from ledger.core.upcasting import UpcasterRegistry
from ledger.infrastructure.upcasters import registry as _default_registry

logger = logging.getLogger(__name__)


class EventStore:
    def __init__(
        self,
        pool: asyncpg.Pool,
        upcaster_registry: UpcasterRegistry | None = None,
    ):
        self._pool = pool
        self._registry = upcaster_registry if upcaster_registry is not None else _default_registry

    def _apply_upcasting(self, data: dict[str, Any]) -> dict[str, Any]:
        """Applies registered upcasters to a raw event dict in-memory.

        The DB row is never modified — upcasting is purely a read-time transform.
        """
        version, payload = self._registry.upcast(
            event_type=data["event_type"],
            event_version=int(data["event_version"]),
            payload=data["payload"],
            recorded_at=data["recorded_at"],
        )
        data = dict(data)
        data["event_version"] = version
        data["payload"] = payload
        return data

    async def _build_session_model_cache(self, session_ids: list[str]) -> dict[str, str]:
        """Loads model_version for each AgentSession stream from its AgentContextLoaded event.

        Used to populate _session_model_cache for DecisionGenerated v1→v2 upcasting.
        Only queries sessions not already resolved; returns {session_id: model_version}.
        """
        if not session_ids:
            return {}
        rows = await self._pool.fetch(
            """
            SELECT DISTINCT ON (stream_id) stream_id, payload
            FROM events
            WHERE stream_id = ANY($1) AND event_type = 'AgentContextLoaded'
            ORDER BY stream_id, stream_position ASC
            """,
            session_ids,
        )
        cache: dict[str, str] = {}
        for row in rows:
            payload = row["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            mv = payload.get("model_version")
            if mv:
                # Key by stream_id (for upcaster) AND by agent_id (for projections)
                # so both lookup styles resolve without any string parsing.
                cache[str(row["stream_id"])] = str(mv)
                agent_id = payload.get("agent_id")
                if agent_id:
                    cache[str(agent_id)] = str(mv)
        return cache

    async def _inject_session_cache(self, data: dict[str, Any]) -> dict[str, Any]:
        """For v1 DecisionGenerated events, fetches and injects _session_model_cache
        so the upcaster can reconstruct model_versions{} from real DB data."""
        if (
            data["event_type"] == "DecisionGenerated"
            and int(data["event_version"]) == 1
            and "model_versions" not in data["payload"]
        ):
            sessions: list[str] = data["payload"].get("contributing_agent_sessions", [])
            if sessions:
                cache = await self._build_session_model_cache(sessions)
                data = dict(data)
                data["payload"] = {**data["payload"], "_session_model_cache": cache}
        return data

    async def append(
        self,
        stream_id: str,
        events: list[BaseEvent],
        expected_version: int,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> int:
        """
        Atomically appends events to stream_id.
        Raises OptimisticConcurrencyError if stream version != expected_version.
        Writes to outbox in same transaction.
        """
        async with self._pool.acquire() as conn, conn.transaction():
            # 1. Lock stream and check version
            # -1 means new stream expected
            if expected_version == -1:
                # Try to insert into event_streams, will fail if already exists
                # We use a row lock for existing streams
                agg_type = stream_id.split("-")[0]
                try:
                    await conn.execute(
                        "INSERT INTO event_streams (stream_id, aggregate_type, current_version) "
                        "VALUES ($1, $2, 0)",
                        stream_id,
                        agg_type,
                    )
                except asyncpg.UniqueViolationError as err:
                    raise OptimisticConcurrencyError(
                        stream_id=stream_id,
                        expected_version=-1,
                        actual_version=0,  # Simplified
                    ) from err
                current_v = 0
            else:
                row = await conn.fetchrow(
                    "SELECT current_version FROM event_streams WHERE stream_id = $1 FOR UPDATE",
                    stream_id,
                )
                if not row:
                    raise OptimisticConcurrencyError(
                        stream_id=stream_id, expected_version=expected_version, actual_version=-1
                    )
                current_v = row["current_version"]
                if current_v != expected_version:
                    raise OptimisticConcurrencyError(
                        stream_id=stream_id,
                        expected_version=expected_version,
                        actual_version=current_v,
                    )

            # 2. Append events
            new_v = current_v
            for event in events:
                new_v += 1
                event_id = await conn.fetchval(
                    """
                        INSERT INTO events 
                        (stream_id, stream_position, event_type, event_version, payload, metadata)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        RETURNING event_id
                        """,
                    stream_id,
                    new_v,
                    event.event_type,
                    event.event_version,
                    json.dumps(event.payload),
                    json.dumps(
                        {
                            **event.metadata,
                            "correlation_id": correlation_id,
                            "causation_id": causation_id,
                        }
                    ),
                )
                logger.info(
                    " Store: Appended %s to %s at pos %d",
                    event.event_type,
                    stream_id,
                    new_v,
                )

                # 3. Write to outbox
                await conn.execute(
                    "INSERT INTO outbox (event_id, destination, payload) VALUES ($1, $2, $3)",
                    event_id,
                    "ALL",
                    json.dumps(event.payload),
                )

            # 4. Update stream version
            await conn.execute(
                "UPDATE event_streams SET current_version = $1 WHERE stream_id = $2",
                new_v,
                stream_id,
            )

            return int(new_v)

        return -1

    async def load_stream(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Loads events from a specific stream, in order."""
        query = "SELECT * FROM events WHERE stream_id = $1 AND stream_position > $2"
        params = [stream_id, from_position]

        if to_position:
            query += " AND stream_position <= $3"
            params.append(to_position)

        query += " ORDER BY stream_position ASC"

        rows = await self._pool.fetch(query, *params)
        events = []
        for row in rows:
            data = dict(row)
            payload_val = data["payload"]
            metadata_val = data["metadata"]
            if isinstance(payload_val, str):
                data["payload"] = json.loads(payload_val)
            if isinstance(metadata_val, str):
                data["metadata"] = json.loads(metadata_val)
            data = await self._inject_session_cache(data)
            data = self._apply_upcasting(data)
            events.append(StoredEvent.model_validate(data))
        return events

    async def load_all(
        self,
        from_global_position: int = 0,
        event_types: list[str] | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[StoredEvent]:
        """Async generator for replaying all events across all streams."""
        current_pos = from_global_position
        while True:
            query = "SELECT * FROM events WHERE global_position > $1"
            params: list[Any] = [current_pos]

            if event_types:
                query += f" AND event_type = ANY(${len(params) + 1})"
                params.append(event_types)

            query += f" ORDER BY global_position ASC LIMIT ${len(params) + 1}"
            params.append(batch_size)

            rows = await self._pool.fetch(query, *params)
            if not rows:
                break

            for row in rows:
                data = dict(row)
                p_str = data["payload"]
                m_str = data["metadata"]
                if isinstance(p_str, str):
                    data["payload"] = json.loads(p_str)
                if isinstance(m_str, str):
                    data["metadata"] = json.loads(m_str)
                data = await self._inject_session_cache(data)
                data = self._apply_upcasting(data)
                event = StoredEvent.model_validate(data)
                yield event
                current_pos = int(event.global_position)

    async def load_stream_raw(
        self,
        stream_id: str,
        from_position: int = 0,
        to_position: int | None = None,
    ) -> list[StoredEvent]:
        """Loads events from a stream without applying upcasting.

        Used by the audit chain so hashes are computed over the exact bytes
        stored in the DB — making the chain independently verifiable without
        needing to run the upcaster pipeline.
        """
        query = "SELECT * FROM events WHERE stream_id = $1 AND stream_position > $2"
        params: list[Any] = [stream_id, from_position]

        if to_position:
            query += " AND stream_position <= $3"
            params.append(to_position)

        query += " ORDER BY stream_position ASC"

        rows = await self._pool.fetch(query, *params)
        events = []
        for row in rows:
            data = dict(row)
            payload_val = data["payload"]
            metadata_val = data["metadata"]
            if isinstance(payload_val, str):
                data["payload"] = json.loads(payload_val)
            if isinstance(metadata_val, str):
                data["metadata"] = json.loads(metadata_val)
            # No _inject_session_cache, no _apply_upcasting
            events.append(StoredEvent.model_validate(data))
        return events

    async def stream_version(self, stream_id: str) -> int:
        version = await self._pool.fetchval(
            "SELECT current_version FROM event_streams WHERE stream_id = $1", stream_id
        )
        if version is None:
            return -1
        # Explicitly cast to satisfy Pyre's SupportsIndex/SupportsInt requirements
        return int(version)

    async def archive_stream(self, stream_id: str) -> None:
        await self._pool.execute(
            "UPDATE event_streams SET archived_at = NOW() WHERE stream_id = $1", stream_id
        )

    async def get_stream_metadata(self, stream_id: str) -> StreamMetadata:
        row = await self._pool.fetchrow(
            "SELECT * FROM event_streams WHERE stream_id = $1", stream_id
        )
        if not row:
            raise ValueError(f"Stream {stream_id} not found")
        data = dict(row)
        metadata_val = data.get("metadata")
        if isinstance(metadata_val, str):
            data["metadata"] = json.loads(metadata_val)
        return StreamMetadata.model_validate(data)
