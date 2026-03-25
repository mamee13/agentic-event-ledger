"""Shard coordinator for distributed ProjectionDaemon instances.

Each daemon node acquires a PostgreSQL advisory lock for its assigned shard
before polling events.  A heartbeat loop keeps the lock alive; a coordinator
scan reclaims shards whose heartbeat has gone stale.

Advisory lock key: hashtext(projection_name || ':' || shard_id)  (32-bit int)
This fits inside pg_try_advisory_lock(bigint) without collision risk for the
small number of shards used in practice (< 64).
"""

import asyncio
import contextlib
import hashlib
import logging
import socket
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger(__name__)

# A shard whose heartbeat is older than this is considered dead.
HEARTBEAT_TTL_SECONDS = 15
# How often the owning node refreshes its heartbeat.
HEARTBEAT_INTERVAL_SECONDS = 5


def _advisory_key(projection_name: str, shard_id: str) -> int:
    """Stable 63-bit advisory lock key derived from projection + shard names.

    Uses the first 8 bytes of SHA-256 so the key space is well-distributed
    and deterministic across restarts.  The result is masked to a positive
    signed 64-bit integer so Postgres accepts it as a bigint.
    """
    raw = hashlib.sha256(f"{projection_name}:{shard_id}".encode()).digest()
    value = int.from_bytes(raw[:8], "big")
    # Mask to positive signed 64-bit range
    return value & 0x7FFF_FFFF_FFFF_FFFF


@dataclass
class ShardAssignment:
    shard_id: str
    projection_name: str
    global_pos_from: int
    global_pos_to: int | None  # None = open-ended tail shard


@dataclass
class ShardCoordinator:
    """Manages advisory lock acquisition, heartbeating, and stale-shard reclaim.

    Usage::

        coordinator = ShardCoordinator(pool, node_id="worker-1")
        assignment = await coordinator.try_acquire_shard(
            ShardAssignment("shard-0", "ApplicationSummary", 0, None)
        )
        if assignment:
            asyncio.create_task(coordinator.heartbeat_loop(assignment))
            # ... process events ...
            await coordinator.release_shard(assignment)
    """

    pool: asyncpg.Pool
    node_id: str = field(default_factory=lambda: socket.gethostname())

    async def try_acquire_shard(
        self, assignment: ShardAssignment, conn: asyncpg.Connection
    ) -> bool:
        """Attempts to acquire the advisory lock for *assignment*.

        Returns True if the lock was acquired (this node now owns the shard),
        False if another node already holds it.

        The caller is responsible for keeping *conn* open for the lifetime of
        the lock — advisory locks are connection-scoped in Postgres.
        """
        key = _advisory_key(assignment.projection_name, assignment.shard_id)
        acquired: bool = await conn.fetchval("SELECT pg_try_advisory_lock($1)", key)
        if not acquired:
            return False

        # Register / refresh ownership in the shards table
        await conn.execute(
            """
            INSERT INTO projection_shards
              (shard_id, projection_name, assigned_node, heartbeat_at,
               global_pos_from, global_pos_to)
            VALUES ($1, $2, $3, NOW(), $4, $5)
            ON CONFLICT (shard_id, projection_name) DO UPDATE SET
              assigned_node   = EXCLUDED.assigned_node,
              heartbeat_at    = NOW(),
              global_pos_from = EXCLUDED.global_pos_from,
              global_pos_to   = EXCLUDED.global_pos_to
            """,
            assignment.shard_id,
            assignment.projection_name,
            self.node_id,
            assignment.global_pos_from,
            assignment.global_pos_to,
        )
        logger.info(
            "Node %s acquired shard %s/%s",
            self.node_id,
            assignment.projection_name,
            assignment.shard_id,
        )
        return True

    async def release_shard(self, assignment: ShardAssignment, conn: asyncpg.Connection) -> None:
        """Releases the advisory lock and removes the shard registration."""
        key = _advisory_key(assignment.projection_name, assignment.shard_id)
        await conn.execute("SELECT pg_advisory_unlock($1)", key)
        await conn.execute(
            "DELETE FROM projection_shards WHERE shard_id = $1 AND projection_name = $2",
            assignment.shard_id,
            assignment.projection_name,
        )
        logger.info(
            "Node %s released shard %s/%s",
            self.node_id,
            assignment.projection_name,
            assignment.shard_id,
        )

    async def refresh_heartbeat(self, assignment: ShardAssignment) -> bool:
        """Updates heartbeat_at for the owned shard.  Called by heartbeat_loop.

        Returns True if the update succeeded, False if the shard registration
        no longer exists for this node (e.g. it was reclaimed).
        """
        rows_affected = await self.pool.execute(
            """
            UPDATE projection_shards SET heartbeat_at = NOW()
            WHERE shard_id = $1 AND projection_name = $2 AND assigned_node = $3
            """,
            assignment.shard_id,
            assignment.projection_name,
            self.node_id,
        )
        return bool(rows_affected == "UPDATE 1")

    async def heartbeat_loop(
        self,
        assignment: ShardAssignment,
        stop_event: asyncio.Event,
    ) -> None:
        """Runs until *stop_event* is set or heartbeats fail (e.g. reclaimed)."""
        while not stop_event.is_set():
            try:
                success = await self.refresh_heartbeat(assignment)
                if not success:
                    logger.error(
                        "Heartbeat failed: shard %s/%s registration missing "
                        "(reclaimed?). Stopping.",
                        assignment.projection_name,
                        assignment.shard_id,
                    )
                    stop_event.set()
                    break
            except Exception:
                logger.exception(
                    "Heartbeat refresh could not connect for shard %s/%s",
                    assignment.projection_name,
                    assignment.shard_id,
                )

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )

    async def reclaim_stale_shards(self) -> list[ShardAssignment]:
        """Finds shards whose heartbeat has expired and deletes their registrations.

        Returns the list of reclaimed assignments so callers can log / re-assign.
        The advisory lock held by the dead node will be released automatically
        when that node's connection drops; we only clean up the metadata here.
        """
        rows = await self.pool.fetch(
            """
            DELETE FROM projection_shards
            WHERE heartbeat_at < NOW() - ($1 || ' seconds')::interval
            RETURNING shard_id, projection_name, global_pos_from, global_pos_to
            """,
            str(HEARTBEAT_TTL_SECONDS),
        )
        reclaimed = [
            ShardAssignment(
                shard_id=row["shard_id"],
                projection_name=row["projection_name"],
                global_pos_from=row["global_pos_from"],
                global_pos_to=row["global_pos_to"],
            )
            for row in rows
        ]
        if reclaimed:
            logger.warning("Reclaimed %d stale shards: %s", len(reclaimed), reclaimed)
        return reclaimed

    async def get_shard_checkpoint(self, projection_name: str, shard_id: str) -> int:
        res = await self.pool.fetchval(
            """
            SELECT last_position FROM projection_shard_checkpoints
            WHERE projection_name = $1 AND shard_id = $2
            """,
            projection_name,
            shard_id,
        )
        return int(res) if res is not None else 0

    async def update_shard_checkpoint(
        self, projection_name: str, shard_id: str, position: int
    ) -> None:
        await self.pool.execute(
            """
            INSERT INTO projection_shard_checkpoints
              (projection_name, shard_id, last_position, updated_at)
            VALUES ($1, $2, $3, NOW())
            ON CONFLICT (projection_name, shard_id) DO UPDATE SET
              last_position = EXCLUDED.last_position,
              updated_at    = NOW()
            """,
            projection_name,
            shard_id,
            position,
        )

    @staticmethod
    def is_event_in_shard(global_position: int, assignment: ShardAssignment) -> bool:
        """Returns True if *global_position* falls within the shard's range."""
        return global_position >= assignment.global_pos_from and (
            assignment.global_pos_to is None or global_position <= assignment.global_pos_to
        )
