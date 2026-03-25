import logging

import asyncpg

from ledger.core.models import StoredEvent
from ledger.infrastructure.projections.base import BaseProjection, resolve_model_version

logger = logging.getLogger(__name__)


class ApplicationSummaryProjection(BaseProjection):
    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool

    @property
    def projection_name(self) -> str:
        return "ApplicationSummary"

    @property
    def subscribed_events(self) -> list[str]:
        return [
            "ApplicationSubmitted",
            "CreditAnalysisRequested",
            "CreditAnalysisCompleted",
            "ComplianceCheckRequested",
            "ComplianceRulePassed",
            "ComplianceRuleFailed",
            "DecisionGenerated",
            "HumanReviewCompleted",
            "ApplicationApproved",
            "ApplicationDeclined",
            "FraudScreeningCompleted",
        ]

    async def handle_event(
        self, event: StoredEvent, conn: asyncpg.Connection | None = None
    ) -> None:
        if event.stream_id.startswith("audit-"):
            # Audit streams are duplicates for integrity; do not drive summaries.
            return
        payload = event.payload
        app_id = payload.get("application_id")
        if not app_id:
            return

        if conn:
            await self._handle_event_with_conn(event, conn)
        else:
            async with self._pool.acquire() as c:
                await self._handle_event_with_conn(event, c)

    async def _handle_event_with_conn(self, event: StoredEvent, conn: asyncpg.Connection) -> None:
        e_type = event.event_type
        payload = event.payload
        app_id = payload.get("application_id")

        if e_type == "ApplicationSubmitted":
            applicant_id = payload.get("applicant_id")
            if not applicant_id:
                logger.warning("ApplicationSubmitted event missing applicant_id for app %s", app_id)
                return

            await conn.execute(
                """
                INSERT INTO projection_application_summary 
                (application_id, state, applicant_id, requested_amount_usd, 
                 last_event_type, last_event_at)
                VALUES ($1, 'SUBMITTED', $2, $3, $4, $5)
                ON CONFLICT (application_id) DO UPDATE SET
                    state = EXCLUDED.state,
                    applicant_id = EXCLUDED.applicant_id,
                    requested_amount_usd = EXCLUDED.requested_amount_usd,
                    last_event_type = EXCLUDED.last_event_type,
                    last_event_at = EXCLUDED.last_event_at
                """,
                app_id,
                applicant_id,
                float(payload.get("requested_amount_usd", 0.0)),
                e_type,
                event.recorded_at,
            )

        elif e_type == "CreditAnalysisRequested":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = 'AWAITING_ANALYSIS', last_event_type = $1, last_event_at = $2
                WHERE application_id = $3
                """,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "CreditAnalysisCompleted":
            agent_session = f"agent-{payload.get('agent_id')}-{payload.get('session_id')}"
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = 'ANALYSIS_COMPLETE', 
                    risk_tier = $1,
                    agent_sessions_completed = CASE
                        WHEN $2 = ANY(agent_sessions_completed) THEN agent_sessions_completed
                        ELSE array_append(agent_sessions_completed, $2)
                    END,
                    last_event_type = $3, 
                    last_event_at = $4
                WHERE application_id = $5
                """,
                payload.get("risk_tier"),
                agent_session,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "ComplianceCheckRequested":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = 'COMPLIANCE_REVIEW', last_event_type = $1, last_event_at = $2
                WHERE application_id = $3
                """,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "ComplianceRulePassed":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET compliance_status = 'PASSED', 
                    state = 'PENDING_DECISION',
                    last_event_type = $1, last_event_at = $2
                WHERE application_id = $3
                """,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "ComplianceRuleFailed":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET compliance_status = 'FAILED', 
                    last_event_type = $1, last_event_at = $2
                WHERE application_id = $3
                """,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "DecisionGenerated":
            agent_id = payload.get("orchestrator_agent_id")
            model_versions = payload.get("model_versions", {})
            model_version = resolve_model_version(model_versions, agent_id)
            recommendation = payload.get("recommendation")

            new_state = "REFERRED"
            if recommendation == "APPROVE":
                new_state = "APPROVED_PENDING_HUMAN"
            elif recommendation == "DECLINE":
                new_state = "DECLINED_PENDING_HUMAN"

            await conn.execute(
                """
                UPDATE projection_application_summary SET 
                    state = $2,
                    decision = $3,
                    orchestrator_agent_id = $4,
                    orchestrator_model_version = $5,
                    last_event_type = $6,
                    last_event_at = $7
                WHERE application_id = $1
                """,
                app_id,
                new_state,
                recommendation,
                agent_id,
                model_version,
                e_type,
                event.recorded_at,
            )

        elif e_type == "HumanReviewCompleted":
            final_decision = payload.get("final_decision")
            new_state = self._get_state_from_final_decision(final_decision)

            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = $1, 
                    human_reviewer_id = $2,
                    last_event_type = $3, 
                    last_event_at = $4
                WHERE application_id = $5
                """,
                new_state,
                payload.get("reviewer_id"),
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "ApplicationApproved":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = 'FINAL_APPROVED', 
                    approved_amount_usd = $1,
                    final_decision_at = $2,
                    last_event_type = $3, 
                    last_event_at = $4
                WHERE application_id = $5
                """,
                float(payload.get("approved_amount_usd", 0.0)),
                event.recorded_at,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "ApplicationDeclined":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET state = 'FINAL_DECLINED', 
                    final_decision_at = $1,
                    last_event_type = $2, 
                    last_event_at = $3
                WHERE application_id = $4
                """,
                event.recorded_at,
                e_type,
                event.recorded_at,
                app_id,
            )

        elif e_type == "FraudScreeningCompleted":
            await conn.execute(
                """
                UPDATE projection_application_summary 
                SET fraud_score = $1, 
                    last_event_type = $2, 
                    last_event_at = $3
                WHERE application_id = $4
                """,
                float(payload.get("fraud_score", 0.0)),
                e_type,
                event.recorded_at,
                app_id,
            )

    def _get_state_from_final_decision(self, decision: str | None) -> str:
        if decision == "APPROVE":
            return "FINAL_APPROVED"
        if decision == "DECLINE":
            return "FINAL_DECLINED"
        return "REFERRED"
