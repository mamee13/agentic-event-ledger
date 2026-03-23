import asyncio
import logging
from collections.abc import Sequence

import asyncpg

from ledger.infrastructure.projections.base import BaseProjection
from ledger.infrastructure.store import EventStore

logger = logging.getLogger(__name__)


class ProjectionDaemon:
    def __init__(
        self,
        store: EventStore,
        projections: Sequence[BaseProjection],
        pool: asyncpg.Pool,
        batch_size: int = 100,
        max_retries: int = 3,
    ):
        self._store = store
        self._projections = projections
        self._pool = pool
        self._batch_size = batch_size
        self._max_retries = max_retries
        self._is_running = False

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        """Polls the event store forever and processes new events."""
        self._is_running = True
        logger.info("ProjectionDaemon started with %d projections", len(self._projections))

        while self._is_running:
            try:
                processed_count = await self._process_batch()
                if processed_count == 0:
                    await asyncio.sleep(poll_interval_ms / 1000.0)
                else:
                    logger.debug(" Daemon: Processed %d events in batch", processed_count)
            except Exception:
                logger.exception("Error in ProjectionDaemon loop")
                await asyncio.sleep(poll_interval_ms / 1000.0)

    async def _process_batch(self) -> int:
        """Loads and processes a batch of events efficiently."""
        # 1. Load current checkpoints and connections
        checkpoints = {}
        conns = {}
        for p in self._projections:
            checkpoints[p.projection_name] = await self._get_checkpoint(p.projection_name)
            conns[p.projection_name] = await self._pool.acquire()

        lowest_pos = min(checkpoints.values()) if checkpoints else 0

        # 2. Load events (limit managed by store.load_all)
        count = 0
        try:
            async for event in self._store.load_all(
                from_global_position=lowest_pos, batch_size=self._batch_size
            ):
                count += 1
                logger.info(
                    " Daemon: Processing [%d] %s (stream: %s)",
                    event.global_position,
                    event.event_type,
                    event.stream_id,
                )
                for projection in self._projections:
                    # Advance checkpoint for non-subscribed events so we don't re-scan them.
                    # Must happen before the subscription check so the position is always
                    # advanced regardless of whether the projection cares about this event.
                    if event.global_position > checkpoints[projection.projection_name]:
                        checkpoints[projection.projection_name] = event.global_position

                    # Subscription check
                    if event.event_type not in projection.subscribed_events:
                        continue

                    # Handle event with retries
                    retries = 0
                    success = False
                    while retries <= self._max_retries:
                        try:
                            await projection.handle_event(
                                event, conn=conns[projection.projection_name]
                            )
                            logger.debug(
                                " Daemon: %s updated to pos %d",
                                projection.projection_name,
                                event.global_position,
                            )
                            success = True
                            break
                        except Exception:
                            retries += 1
                            if retries <= self._max_retries:
                                await asyncio.sleep(0.1 * retries)

                    if not success:
                        logger.error(
                            "Projection %s failed event %s at %d after %d retries. SKIPPING.",
                            projection.projection_name,
                            event.event_type,
                            event.global_position,
                            self._max_retries,
                            exc_info=True,
                        )

            # 3. Update all checkpoints in DB once per batch
            for name, pos in checkpoints.items():
                await self._update_checkpoint(name, pos)

        finally:
            # Release all connections to the pool
            for conn in conns.values():
                await self._pool.release(conn)

        return count

    async def _get_checkpoint(self, projection_name: str) -> int:
        """Gets the checkpoint for a projection."""
        res = await self._pool.fetchval(
            "SELECT last_position FROM projection_checkpoints WHERE projection_name = $1",
            projection_name,
        )
        return int(res) if res is not None else 0

    async def _update_checkpoint(self, projection_name: str, position: int) -> None:
        """Updates the checkpoint for a projection."""
        await self._pool.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
            VALUES ($1, $2, NOW())
            ON CONFLICT (projection_name) DO UPDATE 
            SET last_position = EXCLUDED.last_position, updated_at = NOW()
            """,
            projection_name,
            position,
        )

    async def get_lag(self, projection_name: str) -> int:
        """Returns the lag for a specific projection."""
        last_pos = await self._get_checkpoint(projection_name)
        max_global = await self._pool.fetchval("SELECT MAX(global_position) FROM events")
        if max_global is None:
            return 0
        return int(max_global) - last_pos

    def stop(self) -> None:
        self._is_running = False
