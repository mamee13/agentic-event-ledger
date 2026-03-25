import json
import logging
from typing import Any

import asyncpg

from ledger.core.models import StoredEvent
from ledger.infrastructure.projections.base import BaseProjection

logger = logging.getLogger(__name__)

_PARTIAL_EVENT_TYPES = {
    "CreditAnalysisRequested",
    "FraudScreeningRequested",
    "ComplianceCheckRequested",
    "AgentContextLoaded",
}


class AgentSessionViewProjection(BaseProjection):
    """Projection that materializes AgentSession streams into a query table."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def projection_name(self) -> str:
        return "AgentSessionView"

    @property
    def subscribed_events(self) -> list[str]:
        return [
            "AgentContextLoaded",
            "DecisionOrchestratorSessionStarted",
            "CreditAnalysisCompleted",
            "FraudScreeningCompleted",
            "DecisionGenerated",
            "SessionTerminated",
            "AgentSessionClosed",
        ]

    async def handle_event(
        self, event: StoredEvent, conn: asyncpg.Connection | None = None
    ) -> None:
        if not event.stream_id.startswith("agent-"):
            return

        if conn:
            await self._handle_event_with_conn(event, conn)
        else:
            async with self._pool.acquire() as c:
                await self._handle_event_with_conn(event, c)

    async def _handle_event_with_conn(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        session_id = event.stream_id
        row = await conn.fetchrow(
            "SELECT * FROM projection_agent_sessions WHERE session_id = $1",
            session_id,
        )

        summary: list[str] = []
        recent: list[dict[str, Any]] = []
        total_events = 0
        last_completed_action: str | None = None
        pending_work: str | None = None
        needs_reconciliation = False
        agent_id: str | None = None
        model_version: str | None = None
        is_active = True

        if row:
            summary = self._load_json_list(row["summary"])
            recent = self._load_json_list(row["recent_events"])
            total_events = int(row["total_events"])
            last_completed_action = row["last_completed_action"]
            pending_work = row["pending_work"]
            needs_reconciliation = bool(row["needs_reconciliation"])
            agent_id = row["agent_id"]
            model_version = row["model_version"]
            is_active = bool(row["is_active"])

        total_events += 1
        entry = {
            "event_type": event.event_type,
            "stream_position": event.stream_position,
            "recorded_at": event.recorded_at.isoformat(),
            "payload": event.payload,
        }
        recent.append(entry)
        if len(recent) > 3:
            overflow = recent.pop(0)
            summary.append(self._summarise(overflow["event_type"], overflow["payload"]))

        if event.event_type in {"AgentContextLoaded", "DecisionOrchestratorSessionStarted"}:
            agent_id = str(event.payload.get("agent_id", agent_id or "unknown"))
            model_version = str(event.payload.get("model_version", model_version or "unknown"))
            is_active = True
        elif event.event_type in {"SessionTerminated", "AgentSessionClosed"}:
            is_active = False

        if event.event_type in _PARTIAL_EVENT_TYPES:
            pending_work = event.event_type
            needs_reconciliation = True
        else:
            last_completed_action = event.event_type
            pending_work = None
            needs_reconciliation = False

        await conn.execute(
            """
            INSERT INTO projection_agent_sessions
            (session_id, agent_id, model_version, is_active, last_completed_action,
             pending_work, needs_reconciliation, total_events, summary, recent_events, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                agent_id = EXCLUDED.agent_id,
                model_version = EXCLUDED.model_version,
                is_active = EXCLUDED.is_active,
                last_completed_action = EXCLUDED.last_completed_action,
                pending_work = EXCLUDED.pending_work,
                needs_reconciliation = EXCLUDED.needs_reconciliation,
                total_events = EXCLUDED.total_events,
                summary = EXCLUDED.summary,
                recent_events = EXCLUDED.recent_events,
                updated_at = NOW()
            """,
            session_id,
            agent_id,
            model_version,
            is_active,
            last_completed_action,
            pending_work,
            needs_reconciliation,
            total_events,
            json.dumps(summary),
            json.dumps(recent),
        )

    def _summarise(self, event_type: str, payload: dict[str, Any]) -> str:
        app = payload.get("application_id", "")
        suffix = f" for {app}" if app else ""
        return f"{event_type}{suffix}"

    def _load_json_list(self, value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, str):
            data = json.loads(value)
            return list(data) if isinstance(data, list) else []
        return list(value)
