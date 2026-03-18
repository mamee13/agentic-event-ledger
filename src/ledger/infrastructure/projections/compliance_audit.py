import json
from datetime import datetime
from typing import TYPE_CHECKING

import asyncpg

from ledger.core.models import StoredEvent
from ledger.infrastructure.projections.base import BaseProjection

if TYPE_CHECKING:
    from ledger.infrastructure.store import EventStore


class ComplianceAuditViewProjection(BaseProjection):
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def projection_name(self) -> str:
        return "ComplianceAuditView"

    @property
    def subscribed_events(self) -> list[str]:
        return [
            "ComplianceCheckRequested",
            "ComplianceRulePassed",
            "ComplianceRuleFailed",
        ]

    async def handle_event(
        self, event: StoredEvent, conn: asyncpg.Connection | None = None
    ) -> None:
        app_id = event.payload.get("application_id")
        if not app_id:
            return

        if conn:
            await self._handle_event_with_conn(event, conn)
        else:
            async with self._pool.acquire() as c:
                await self._handle_event_with_conn(event, c)

    async def _handle_event_with_conn(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        app_id: str | None = event.payload.get("application_id")
        if not app_id:
            return
        await conn.execute(
            """
            INSERT INTO projection_compliance_history 
            (application_id, event_type, payload, global_position, recorded_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (application_id, global_position) DO NOTHING
            """,
            app_id,
            event.event_type,
            json.dumps(event.payload),
            event.global_position,
            event.recorded_at,
        )

        # Snapshot every 50 events
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM projection_compliance_history WHERE application_id = $1",
            app_id,
        )
        if count % 50 == 0:
            current_state = await self.get_current_compliance(app_id, conn=conn)
            await conn.execute(
                """
                INSERT INTO projection_snapshots
                (projection_name, entity_id, global_position, state)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (projection_name, entity_id, global_position) DO UPDATE 
                SET state = EXCLUDED.state
                """,
                self.projection_name,
                app_id,
                event.global_position,
                json.dumps(current_state),
            )

    async def get_current_compliance(
        self, application_id: str, conn: asyncpg.Connection | None = None
    ) -> dict[str, object]:
        """Returns the full current compliance record using snapshots + replay."""
        if conn is None:
            async with self._pool.acquire() as conn:
                return await self._get_compliance_logic(application_id, conn, None)
        return await self._get_compliance_logic(application_id, conn, None)

    async def get_compliance_at(
        self, application_id: str, timestamp: datetime, conn: asyncpg.Connection | None = None
    ) -> dict[str, object]:
        """Returns compliance state at a specific time."""
        if conn is None:
            async with self._pool.acquire() as conn:
                return await self._get_compliance_logic(application_id, conn, timestamp)
        return await self._get_compliance_logic(application_id, conn, timestamp)

    async def _get_compliance_logic(
        self, application_id: str, conn: asyncpg.Connection, timestamp: datetime | None
    ) -> dict[str, object]:
        """Internal logic for rehydration with optional time filter."""
        snapshot_query = (
            "SELECT state, global_position FROM projection_snapshots "
            "WHERE projection_name = $1 AND entity_id = $2 "
        )
        params: list[object] = [self.projection_name, application_id]
        if timestamp:
            snapshot_query += "AND created_at <= $3 "
            params.append(timestamp)
        snapshot_query += "ORDER BY global_position DESC LIMIT 1"

        snapshot = await conn.fetchrow(snapshot_query, *params)

        if snapshot:
            state = json.loads(snapshot["state"])
            last_pos = snapshot["global_position"]
            history_query = (
                "SELECT * FROM projection_compliance_history "
                "WHERE application_id = $1 AND global_position > $2 "
            )
            h_params: list[object] = [application_id, last_pos]
            if timestamp:
                history_query += "AND recorded_at <= $3 "
                h_params.append(timestamp)
            history_query += "ORDER BY global_position ASC"
            rows = await conn.fetch(history_query, *h_params)
            return self._rehydrate_compliance(rows, initial_state=state)

        # No snapshot, full replay
        history_query = "SELECT * FROM projection_compliance_history WHERE application_id = $1 "
        h_params = [application_id]
        if timestamp:
            history_query += "AND recorded_at <= $2 "
            h_params.append(timestamp)
        history_query += "ORDER BY global_position ASC"
        rows = await conn.fetch(history_query, *h_params)
        return self._rehydrate_compliance(rows)

    def _rehydrate_compliance(
        self,
        rows: list[asyncpg.Record],
        initial_state: dict[str, object] | None = None,
    ) -> dict[str, object]:
        state: dict[str, object] = initial_state or {
            "application_id": None,
            "rules": {},
            "status": "PENDING",
            "last_updated": None,
        }

        for row in rows:
            payload = json.loads(row["payload"])
            state["application_id"] = row["application_id"]
            state["last_updated"] = row["recorded_at"].isoformat()

            e_type = row["event_type"]
            rules = state.setdefault("rules", {})
            assert isinstance(rules, dict)
            if e_type == "ComplianceCheckRequested":
                state["status"] = "IN_PROGRESS"
            elif e_type == "ComplianceRulePassed":
                rule_id = payload.get("rule_id")
                rules[rule_id] = "PASSED"
            elif e_type == "ComplianceRuleFailed":
                rule_id = payload.get("rule_id")
                rules[rule_id] = "FAILED"

        # Calculate overall status
        rules = state.get("rules", {})
        assert isinstance(rules, dict)
        if rules:
            if all(v == "PASSED" for v in rules.values()):
                state["status"] = "PASSED"
            elif any(v == "FAILED" for v in rules.values()):
                state["status"] = "FAILED"

        return state

    async def get_projection_lag(self) -> int:
        """Returns ms between the latest store event and the last processed event."""
        async with self._pool.acquire() as conn:
            res = await conn.fetchrow(
                """
                SELECT
                    EXTRACT(EPOCH FROM (
                        (SELECT recorded_at FROM events ORDER BY global_position DESC LIMIT 1)
                        -
                        (SELECT recorded_at FROM events e
                         JOIN projection_checkpoints pc ON pc.projection_name = $1
                         WHERE e.global_position = pc.last_position
                         LIMIT 1)
                    )) * 1000 AS lag_ms
                """,
                self.projection_name,
            )
            if res is None or res["lag_ms"] is None:
                return 0
            return max(0, int(res["lag_ms"]))

    async def rebuild_from_scratch(self, store: "EventStore") -> None:
        """Replays all events into a shadow table, then swaps via a view — zero read downtime.

        Live reads go through the `compliance_history_view` view, which always points to
        the current backing table. The swap renames tables and recreates the view atomically
        inside a single transaction, so readers never see a gap.
        """
        async with self._pool.acquire() as conn:
            # 1. Ensure the stable view exists (idempotent)
            await conn.execute(
                """
                DO $$
                BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.views
                        WHERE table_name = 'compliance_history_view'
                    ) THEN
                        EXECUTE 'CREATE VIEW compliance_history_view AS '
                            'SELECT * FROM projection_compliance_history';
                    END IF;
                END$$
                """
            )

            # 2. Build shadow table from scratch
            await conn.execute("DROP TABLE IF EXISTS projection_compliance_history_shadow")
            await conn.execute(
                "CREATE TABLE projection_compliance_history_shadow "
                "(LIKE projection_compliance_history INCLUDING ALL)"
            )

            # 3. Replay all events into shadow table
            async for event in store.load_all(event_types=self.subscribed_events):
                app_id = event.payload.get("application_id")
                if app_id:
                    await conn.execute(
                        """
                        INSERT INTO projection_compliance_history_shadow
                        (application_id, event_type, payload, global_position, recorded_at)
                        VALUES ($1, $2, $3, $4, $5)
                        ON CONFLICT (application_id, global_position) DO NOTHING
                        """,
                        app_id,
                        event.event_type,
                        json.dumps(event.payload),
                        event.global_position,
                        event.recorded_at,
                    )

            # 4. Atomic swap: rename tables + recreate view in one transaction.
            #    Readers using the view see old data until commit, then new data immediately.
            async with conn.transaction():
                await conn.execute(
                    "ALTER TABLE projection_compliance_history "
                    "RENAME TO projection_compliance_history_old"
                )
                await conn.execute(
                    "ALTER TABLE projection_compliance_history_shadow "
                    "RENAME TO projection_compliance_history"
                )
                await conn.execute("DROP VIEW IF EXISTS compliance_history_view")
                await conn.execute(
                    "CREATE VIEW compliance_history_view AS "
                    "SELECT * FROM projection_compliance_history"
                )

            # 5. Drop old table outside the swap transaction
            await conn.execute("DROP TABLE IF EXISTS projection_compliance_history_old")

            # 6. Reset checkpoint
            max_pos = await conn.fetchval(
                "SELECT MAX(global_position) FROM projection_compliance_history"
            )
            await conn.execute(
                """
                INSERT INTO projection_checkpoints (projection_name, last_position, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (projection_name) DO UPDATE
                SET last_position = EXCLUDED.last_position, updated_at = NOW()
                """,
                self.projection_name,
                max_pos or 0,
            )
