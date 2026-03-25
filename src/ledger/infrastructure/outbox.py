"""Outbox relay for guaranteed event delivery.

Polls unpublished outbox rows and invokes a user-supplied publisher. Marks
rows as published only after successful delivery.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

Publisher = Callable[[dict[str, Any]], Awaitable[None]]


class OutboxRelay:
    def __init__(
        self,
        pool: asyncpg.Pool,
        publish: Publisher,
        batch_size: int = 100,
        poll_interval_ms: int = 200,
        max_attempts: int = 5,
    ) -> None:
        self._pool = pool
        self._publish = publish
        self._batch_size = batch_size
        self._poll_interval_ms = poll_interval_ms
        self._max_attempts = max_attempts
        self._running = False

    async def run_forever(self) -> None:
        self._running = True
        logger.info("OutboxRelay started (batch=%d)", self._batch_size)
        while self._running:
            try:
                processed = await self._drain_batch()
                if processed == 0:
                    await asyncio.sleep(self._poll_interval_ms / 1000.0)
            except Exception:
                logger.exception("OutboxRelay error; backing off")
                await asyncio.sleep(self._poll_interval_ms / 1000.0)

    def stop(self) -> None:
        self._running = False

    async def _drain_batch(self) -> int:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, event_id, destination, payload, attempts
                FROM outbox
                WHERE published_at IS NULL AND attempts < $1
                ORDER BY created_at ASC
                LIMIT $2
                FOR UPDATE SKIP LOCKED
                """,
                self._max_attempts,
                self._batch_size,
            )
            if not rows:
                return 0

            for row in rows:
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                try:
                    await self._publish(
                        {
                            "id": str(row["id"]),
                            "event_id": str(row["event_id"]),
                            "destination": row["destination"],
                            "payload": payload,
                        }
                    )
                    await conn.execute(
                        "UPDATE outbox SET published_at = NOW() WHERE id = $1",
                        row["id"],
                    )
                except Exception:
                    logger.exception("Outbox publish failed for %s", row["id"])
                    await conn.execute(
                        "UPDATE outbox SET attempts = attempts + 1 WHERE id = $1",
                        row["id"],
                    )
            return len(rows)
