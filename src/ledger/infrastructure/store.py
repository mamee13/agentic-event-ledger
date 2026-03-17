import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg

from ledger.core.errors import OptimisticConcurrencyError
from ledger.core.models import BaseEvent, StoredEvent, StreamMetadata


class EventStore:
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

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
            # asyncpg correctly handles JSONB as dicts if configured or if using certain versions
            # but manually parsing for string results is a safe fallback for now.
            payload_val = data["payload"]
            metadata_val = data["metadata"]
            if isinstance(payload_val, str):
                data["payload"] = json.loads(payload_val)
            if isinstance(metadata_val, str):
                data["metadata"] = json.loads(metadata_val)
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
                event = StoredEvent.model_validate(data)
                yield event
                current_pos = int(event.global_position)

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
