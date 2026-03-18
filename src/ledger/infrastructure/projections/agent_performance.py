import logging

import asyncpg

from ledger.core.models import StoredEvent
from ledger.infrastructure.projections.base import BaseProjection, resolve_model_version

logger = logging.getLogger(__name__)


class AgentPerformanceProjection(BaseProjection):
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def projection_name(self) -> str:
        return "AgentPerformance"

    @property
    def subscribed_events(self) -> list[str]:
        return [
            "AgentContextLoaded",
            "CreditAnalysisCompleted",
            "DecisionGenerated",
            "HumanReviewCompleted",
        ]

    async def handle_event(
        self, event: StoredEvent, conn: asyncpg.Connection | None = None
    ) -> None:
        payload = event.payload

        # Determine agent_id and model_version from payload or metadata
        agent_id = payload.get("agent_id") or payload.get("orchestrator_agent_id")
        model_versions_map: dict[str, str] = payload.get("model_versions", {})

        if conn:
            await self._handle_event_with_conn(event, conn, agent_id, model_versions_map)
        else:
            async with self._pool.acquire() as c:
                await self._handle_event_with_conn(event, c, agent_id, model_versions_map)

    async def _handle_event_with_conn(
        self,
        event: StoredEvent,
        conn: asyncpg.Connection,
        agent_id: str | None,
        model_versions_map: dict[str, str],
    ) -> None:
        e_type = event.event_type
        payload = event.payload

        # Handle AgentContextLoaded specifically to initialize record
        if e_type == "AgentContextLoaded":
            model_version = payload.get("model_version")
            if not agent_id or not model_version:
                return

            await conn.execute(
                """
                INSERT INTO projection_agent_performance 
                (agent_id, model_version, first_seen_at, last_seen_at)
                VALUES ($1, $2, $3, $3)
                ON CONFLICT (agent_id, model_version) DO UPDATE SET
                    last_seen_at = EXCLUDED.last_seen_at
                """,
                agent_id,
                model_version,
                event.recorded_at,
            )
            return

        # For other events, we might have multiple agents involved or just one
        if e_type == "CreditAnalysisCompleted":
            model_version = payload.get("model_version")
            if agent_id and model_version:
                await self._update_metrics(conn, agent_id, model_version, event, analysis_done=True)

        elif e_type == "DecisionGenerated":
            model_version = resolve_model_version(model_versions_map, agent_id)
            if agent_id and model_version:
                recommendation = payload.get("recommendation")
                confidence = float(payload.get("confidence_score", 0.0))

                await self._update_metrics(
                    conn,
                    agent_id,
                    model_version,
                    event,
                    decision_done=True,
                    recommendation=recommendation,
                    confidence=confidence,
                )

        elif e_type == "HumanReviewCompleted":
            row = await conn.fetchrow(
                """
                SELECT orchestrator_agent_id, orchestrator_model_version 
                FROM projection_application_summary WHERE application_id = $1
                """,
                payload.get("application_id"),
            )
            if row and row["orchestrator_agent_id"] and row["orchestrator_model_version"]:
                is_override = payload.get("is_override", False)
                await self._update_metrics(
                    conn,
                    row["orchestrator_agent_id"],
                    row["orchestrator_model_version"],
                    event,
                    human_review_done=True,
                    is_override=is_override,
                )

    async def _update_metrics(
        self,
        conn: asyncpg.Connection,
        agent_id: str,
        model_version: str,
        event: StoredEvent,
        analysis_done: bool = False,
        decision_done: bool = False,
        human_review_done: bool = False,
        is_override: bool = False,
        recommendation: str | None = None,
        confidence: float | None = None,
    ) -> None:
        # First, ensure record exists robustly
        await conn.execute(
            """
            INSERT INTO projection_agent_performance 
            (agent_id, model_version, first_seen_at, last_seen_at)
            VALUES ($1, $2, $3, $3)
            ON CONFLICT (agent_id, model_version) DO NOTHING
            """,
            agent_id,
            model_version,
            event.recorded_at,
        )

        # Update metrics
        updates = ["last_seen_at = $3"]
        params = [agent_id, model_version, event.recorded_at]

        if analysis_done:
            new_analyses = "(analyses_completed + 1)"
            duration = float(event.payload.get("analysis_duration_ms", 0.0))
            if duration > 0:
                updates.append(
                    f"avg_duration_ms = (avg_duration_ms * analyses_completed + "
                    f"${len(params) + 1}) / {new_analyses}"
                )
                params.append(duration)
            updates.append(f"analyses_completed = {new_analyses}")

        if decision_done:
            new_decisions = "decisions_generated + 1"

            if confidence is not None:
                updates.append(
                    f"avg_confidence_score = (avg_confidence_score * decisions_generated + "
                    f"${len(params) + 1}) / ({new_decisions})"
                )
                params.append(confidence)

            if recommendation == "APPROVE":
                updates.append(
                    f"approve_rate = (approve_rate * decisions_generated + 1) / ({new_decisions})"
                )
            else:
                updates.append(
                    f"approve_rate = (approve_rate * decisions_generated) / ({new_decisions})"
                )

            if recommendation == "DECLINE":
                updates.append(
                    f"decline_rate = (decline_rate * decisions_generated + 1) / ({new_decisions})"
                )
            else:
                updates.append(
                    f"decline_rate = (decline_rate * decisions_generated) / ({new_decisions})"
                )

            if recommendation == "REFER":
                updates.append(
                    f"refer_rate = (refer_rate * decisions_generated + 1) / ({new_decisions})"
                )
            else:
                updates.append(
                    f"refer_rate = (refer_rate * decisions_generated) / ({new_decisions})"
                )

            updates.append(f"decisions_generated = {new_decisions}")

        if human_review_done:
            new_reviews = "(human_reviews_total + 1)"
            if is_override:
                updates.append("human_overrides_total = human_overrides_total + 1")
                updates.append(
                    f"human_override_rate = (human_overrides_total + 1)::numeric / {new_reviews}"
                )
            else:
                updates.append(
                    f"human_override_rate = human_overrides_total::numeric / {new_reviews}"
                )

            updates.append(f"human_reviews_total = {new_reviews}")

        query = (
            f"UPDATE projection_agent_performance SET {', '.join(updates)} "
            "WHERE agent_id = $1 AND model_version = $2"
        )
        await conn.execute(query, *params)
