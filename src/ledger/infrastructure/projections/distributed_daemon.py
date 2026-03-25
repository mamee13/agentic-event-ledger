"""Distributed ProjectionDaemon with shard-based coordination.

Extends the single-node ProjectionDaemon with:
- Advisory lock acquisition per shard before polling
- Per-shard checkpointing (projection_shard_checkpoints table)
- Heartbeat loop to keep shard ownership alive
- Stale-shard reclaim so orphaned shards are recovered within TTL

The existing ProjectionDaemon is untouched — this class is a drop-in
replacement for multi-node deployments.  Single-node deployments can
continue using ProjectionDaemon directly.
"""

import asyncio
import logging
import socket
from collections.abc import Sequence

import asyncpg

from ledger.infrastructure.projections.base import BaseProjection
from ledger.infrastructure.projections.daemon import ProjectionDaemon
from ledger.infrastructure.projections.shard_coordinator import (
    ShardAssignment,
    ShardCoordinator,
)
from ledger.infrastructure.store import EventStore

logger = logging.getLogger(__name__)


class DistributedProjectionDaemon(ProjectionDaemon):
    """A ProjectionDaemon that uses advisory locks to coordinate across nodes.

    Each instance is responsible for one shard of the global event log.
    Multiple instances can run in parallel without stepping on each other.

    Note: Bounded shards (where ``global_pos_to`` is set) are one-shot consumers.
    Once a daemon reaches the upper bound, it will stop and cannot be restarted
    without creating a new instance (or resetting its internal state).
    """

    def __init__(
        self,
        store: EventStore,
        projections: Sequence[BaseProjection],
        pool: asyncpg.Pool,
        shard_id: str = "shard-0",
        global_pos_from: int = 0,
        global_pos_to: int | None = None,
        node_id: str | None = None,
        batch_size: int = 100,
        max_retries: int = 3,
    ):
        super().__init__(
            store=store,
            projections=projections,
            pool=pool,
            batch_size=batch_size,
            max_retries=max_retries,
        )
        self._shard_id = shard_id
        self._global_pos_from = global_pos_from
        self._global_pos_to = global_pos_to
        self._node_id = node_id or socket.gethostname()
        self._coordinator = ShardCoordinator(pool=pool, node_id=self._node_id)
        # Dedicated connection held for the lifetime of the advisory lock
        self._lock_conn: asyncpg.Connection | None = None
        self._stop_heartbeat = asyncio.Event()
        self._heartbeat_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_forever(self, poll_interval_ms: int = 100) -> None:
        """Acquires the shard lock then delegates to the parent poll loop."""
        self._is_running = True
        self._lock_conn = await self._pool.acquire()
        assignment = ShardAssignment(
            shard_id=self._shard_id,
            projection_name="*",  # coordinator tracks per-node, not per-projection
            global_pos_from=self._global_pos_from,
            global_pos_to=self._global_pos_to,
        )

        # Try to acquire the shard lock; if another node holds it, wait and retry.
        while self._is_running:
            acquired = await self._coordinator.try_acquire_shard(assignment, self._lock_conn)
            if acquired:
                break
            logger.info(
                "Node %s waiting for shard %s (held by another node)",
                self._node_id,
                self._shard_id,
            )
            await asyncio.sleep(poll_interval_ms / 1000.0)

        if not self._is_running:
            # stop() was called before we acquired the lock
            await self._pool.release(self._lock_conn)
            return

        # Start heartbeat loop
        self._stop_heartbeat.clear()
        self._heartbeat_task = asyncio.create_task(
            self._coordinator.heartbeat_loop(assignment, self._stop_heartbeat)
        )

        # Start background stale-shard reclamation task
        reclaim_task = asyncio.create_task(self._reclaim_loop())

        logger.info(
            "DistributedProjectionDaemon node=%s shard=%s range=[%d, %s] started",
            self._node_id,
            self._shard_id,
            self._global_pos_from,
            self._global_pos_to if self._global_pos_to is not None else "inf",
        )

        try:
            # Check stop_heartbeat because heartbeat_loop might set it if reclaimed
            while self._is_running and not self._stop_heartbeat.is_set():
                try:
                    processed_count = await self._process_batch()
                    if processed_count == 0:
                        await asyncio.sleep(poll_interval_ms / 1000.0)
                except Exception:
                    logger.exception("Error in DistributedProjectionDaemon loop")
                    await asyncio.sleep(poll_interval_ms / 1000.0)
        finally:
            logger.info(
                "DistributedProjectionDaemon node=%s shard=%s shutting down",
                self._node_id,
                self._shard_id,
            )
            self._stop_heartbeat.set()
            reclaim_task.cancel()
            if self._heartbeat_task:
                logger.debug("Waiting for heartbeat task...")
                try:
                    await asyncio.wait_for(self._heartbeat_task, timeout=5.0)
                except Exception:
                    logger.warning("Heartbeat task failed to stop gracefully")

            # Ensure reclaim_task is cleaned up to avoid asyncio warnings
            await asyncio.gather(reclaim_task, return_exceptions=True)

            logger.debug("Releasing shard lock...")
            try:
                await self._coordinator.release_shard(assignment, self._lock_conn)
            except Exception:
                logger.warning("Failed to release shard lock")

            logger.debug("Releasing lock connection...")
            await self._pool.release(self._lock_conn)
            logger.info(
                "DistributedProjectionDaemon node=%s shard=%s stopped",
                self._node_id,
                self._shard_id,
            )

    async def _reclaim_loop(self) -> None:
        """Periodically scans for and reclaims stale shards."""
        while self._is_running:
            try:
                await self._coordinator.reclaim_stale_shards()
            except Exception:
                logger.exception("Stale shard reclamation failed")
            # Scan every 15s to match HEARTBEAT_TTL_SECONDS
            await asyncio.sleep(15)

    # ------------------------------------------------------------------
    # Override checkpoint methods to use per-shard checkpoints
    # ------------------------------------------------------------------

    async def _get_checkpoint(self, projection_name: str) -> int:
        db_pos = await self._coordinator.get_shard_checkpoint(projection_name, self._shard_id)
        # We want the first event fetched to be >= global_pos_from.
        # Since load_all(from_pos) returns events > from_pos, we pass global_pos_from - 1.
        # Example: if range starts at 100, we start polling from 99 so we see event 100.
        lower_bound = self._global_pos_from - 1 if self._global_pos_from > 0 else 0
        return max(db_pos, lower_bound)

    async def _update_checkpoint(self, projection_name: str, position: int) -> None:
        await self._coordinator.update_shard_checkpoint(projection_name, self._shard_id, position)

    async def _process_batch(self) -> int:
        """Override to respect shard boundaries (from/to)."""
        checkpoints = {}
        conns = {}
        for p in self._projections:
            checkpoints[p.projection_name] = await self._get_checkpoint(p.projection_name)
            conns[p.projection_name] = await self._pool.acquire()

        lowest_pos = min(checkpoints.values()) if checkpoints else 0

        count = 0
        try:
            async for event in self._store.load_all(
                from_global_position=lowest_pos, batch_size=self._batch_size
            ):
                # 0. Check if we lost our lock mid-batch
                if self._stop_heartbeat.is_set():
                    logger.warning(
                        "Node %s stopping mid-batch (lock lost or daemon stopped)",
                        self._node_id,
                    )
                    break

                # 1. Check if we reached the end of our shard
                if self._global_pos_to is not None and event.global_position > self._global_pos_to:
                    logger.info(
                        "Node %s reached end of shard %s (pos %d > %d)",
                        self._node_id,
                        self._shard_id,
                        event.global_position,
                        self._global_pos_to,
                    )
                    self.stop()
                    break

                # 2. Skip events before our shard (should be handled by lowest_pos, but safe)
                if event.global_position < self._global_pos_from:
                    continue

                count += 1
                for projection in self._projections:
                    if event.global_position > checkpoints[projection.projection_name]:
                        checkpoints[projection.projection_name] = event.global_position

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
                            success = True
                            break
                        except Exception:
                            retries += 1
                            if retries <= self._max_retries:
                                await asyncio.sleep(0.1 * retries)

                    if not success:
                        logger.error(
                            "Projection %s failed event %s at %d. SKIPPING.",
                            projection.projection_name,
                            event.event_type,
                            event.global_position,
                        )

            # 3. Update all checkpoints
            for name, pos in checkpoints.items():
                await self._update_checkpoint(name, pos)

        finally:
            for conn in conns.values():
                await self._pool.release(conn)

        return count
