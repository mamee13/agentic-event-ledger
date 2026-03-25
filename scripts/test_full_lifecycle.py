"""
scripts/test_full_lifecycle.py
==============================
Drives a full loan application lifecycle against the live DB and prints
every step's output. Tests all agent paths: credit, fraud, compliance,
decision, human review, integrity check, regulatory package.

Run:
    PYTHONPATH=src uv run python scripts/test_full_lifecycle.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import uuid

from ledger.application.service import LedgerService
from ledger.core.regulatory_package import generate_regulatory_package
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.parsers import parse_any
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.store import EventStore

# ── IDs used throughout ────────────────────────────────────────────────────────
_RUN = str(uuid.uuid4())[:8]
APP_ID = f"script-{_RUN}"
CREDIT_AGENT = "credit-agent"
CREDIT_SESS = f"credit-sess-{_RUN}"
FRAUD_AGENT = "fraud-agent"
FRAUD_SESS = f"fraud-sess-{_RUN}"
DECISION_AGENT = "decision-agent"
DECISION_SESS = f"decision-sess-{_RUN}"
REVIEWER_ID = "reviewer-script"
FIXTURE_DIR = Path("tests/fixtures/docs/COMP-009")


def section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print("=" * 60)


def show(label: str, data: object) -> None:
    print(f"\n[{label}]")
    print(json.dumps(data, indent=2, default=str))


async def main() -> None:
    pool = await get_pool()
    store = EventStore(pool)
    service = LedgerService(store)
    compliance_proj = ComplianceAuditViewProjection(pool)

    # ── 0. Parse fixture documents ─────────────────────────────────────────────
    section("0. PARSE DOCUMENTS")
    for fname in [
        "financial_summary.csv",
        "application_proposal.pdf",
        "balance_sheet_2024.pdf",
        "income_statement_2024.pdf",
        "financial_statements.xlsx",
    ]:
        fpath = FIXTURE_DIR / fname
        if fpath.exists():
            data = parse_any(str(fpath))
            # Truncate long PDF text for readability
            if isinstance(data, str):
                data = data[:800] + "..." if len(data) > 800 else data
            show(fname, data)
        else:
            print(f"  [SKIP] {fpath} not found")

    # ── 1. Submit application ──────────────────────────────────────────────────
    section("1. SUBMIT APPLICATION")
    await service.submit_application(
        loan_id=APP_ID,
        amount=179_000.0,
        applicant_id="COMP-009",
    )
    v = await store.stream_version(f"loan-{APP_ID}")
    show("submit_application", {"stream": f"loan-{APP_ID}", "version": v, "ok": True})

    # ── 2. Credit agent ────────────────────────────────────────────────────────
    section("2. CREDIT AGENT")
    await service.start_agent_session(
        session_id=CREDIT_SESS,
        agent_id=CREDIT_AGENT,
        model_version="v1.0",
    )
    show(
        "start_agent_session (credit)",
        {"stream": f"agent-{CREDIT_AGENT}-{CREDIT_SESS}", "ok": True},
    )

    await service.request_credit_analysis(loan_id=APP_ID)
    show("request_credit_analysis", {"state": "AWAITING_ANALYSIS", "ok": True})

    await service.record_credit_analysis(
        loan_id=APP_ID,
        agent_id=CREDIT_AGENT,
        session_id=CREDIT_SESS,
        risk_score=0.28,
        reasoning="Strong coverage ratios, low leverage, steady profitability.",
        risk_tier="LOW",
        confidence_score=0.88,
    )
    loan_events = await store.load_stream(f"loan-{APP_ID}")
    show(
        "record_credit_analysis",
        {
            "ok": True,
            "loan_stream_events": [e.event_type for e in loan_events],
        },
    )

    # ── 3. Fraud agent ─────────────────────────────────────────────────────────
    section("3. FRAUD AGENT")
    await service.start_agent_session(
        session_id=FRAUD_SESS,
        agent_id=FRAUD_AGENT,
        model_version="v1.0",
    )
    show("start_agent_session (fraud)", {"stream": f"agent-{FRAUD_AGENT}-{FRAUD_SESS}", "ok": True})

    await service.record_fraud_screening(
        loan_id=APP_ID,
        agent_id=FRAUD_AGENT,
        session_id=FRAUD_SESS,
        fraud_score=0.05,
        screening_result="PASS",
        flags=[],
    )
    show("record_fraud_screening", {"fraud_score": 0.05, "result": "PASS", "ok": True})

    # ── 4. Compliance ──────────────────────────────────────────────────────────
    section("4. COMPLIANCE AGENT")
    await service.request_compliance_check(
        loan_id=APP_ID,
        required_rules=["KYC", "AML"],
    )
    show("request_compliance_check", {"state": "COMPLIANCE_REVIEW", "ok": True})

    await service.record_compliance(loan_id=APP_ID, rule_id="KYC", status="PASSED")
    show("record_compliance KYC", {"rule": "KYC", "status": "PASSED", "ok": True})

    await service.record_compliance(loan_id=APP_ID, rule_id="AML", status="PASSED")
    show("record_compliance AML", {"rule": "AML", "status": "PASSED", "ok": True})

    compliance_state = await compliance_proj.get_current_compliance(APP_ID)
    show("compliance projection state", compliance_state)

    # ── 5. Decision agent ──────────────────────────────────────────────────────
    section("5. DECISION AGENT")
    await service.start_agent_session(
        session_id=DECISION_SESS,
        agent_id=DECISION_AGENT,
        model_version="v1.0",
    )
    show(
        "start_agent_session (decision)",
        {"stream": f"agent-{DECISION_AGENT}-{DECISION_SESS}", "ok": True},
    )

    await service.generate_decision(
        loan_id=APP_ID,
        agent_id=DECISION_AGENT,
        session_id=DECISION_SESS,
        recommendation="APPROVE",
        confidence=0.88,
        contributing_sessions=[
            {"agent_id": CREDIT_AGENT, "session_id": CREDIT_SESS},
            {"agent_id": FRAUD_AGENT, "session_id": FRAUD_SESS},
        ],
    )
    loan_events = await store.load_stream(f"loan-{APP_ID}")
    show(
        "generate_decision",
        {
            "ok": True,
            "recommendation": "APPROVE",
            "confidence": 0.88,
            "loan_stream_events": [e.event_type for e in loan_events],
        },
    )

    # ── 6. Human review ────────────────────────────────────────────────────────
    section("6. HUMAN REVIEW")
    await service.record_human_review(
        loan_id=APP_ID,
        reviewer_id=REVIEWER_ID,
        decision="APPROVE",
        override=False,
    )
    loan_events = await store.load_stream(f"loan-{APP_ID}")
    show(
        "record_human_review",
        {
            "ok": True,
            "final_decision": "APPROVE",
            "reviewer": REVIEWER_ID,
            "loan_stream_events": [e.event_type for e in loan_events],
        },
    )

    # ── 7. Integrity check ─────────────────────────────────────────────────────
    section("7. AUDIT INTEGRITY CHECK")
    # Initialise the audit stream so there are events to hash
    from ledger.core.models import BaseEvent as _BaseEvent

    audit_stream = f"audit-loan-{APP_ID}"
    audit_ver = await store.stream_version(audit_stream)
    if audit_ver == -1:
        await store.append(
            audit_stream,
            [
                _BaseEvent(
                    event_type="AuditStreamInitialised",
                    payload={
                        "entity_id": APP_ID,
                        "entity_type": "loan",
                        "initialised_at": "2026-03-25T00:00:00Z",
                    },
                )
            ],
            expected_version=-1,
        )
    new_hash, prev_hash = await service.run_audit_integrity_check(loan_id=APP_ID)
    show(
        "run_integrity_check",
        {
            "ok": True,
            "integrity_hash": new_hash,
            "previous_hash": prev_hash,
            "chain_non_empty": new_hash
            != "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        },
    )

    # ── 8. Regulatory package ──────────────────────────────────────────────────
    section("8. REGULATORY PACKAGE")
    from datetime import timedelta

    # Fetch current time from Postgres to avoid client/server clock skew causing
    # recorded_at > examination_date and filtering out all events
    db_now = await pool.fetchval("SELECT NOW()")
    examination_date = db_now + timedelta(seconds=1)
    package = await generate_regulatory_package(
        application_id=APP_ID,
        examination_date=examination_date,
        store=store,
        compliance_projection=compliance_proj,
    )
    # Show summary only — full package can be large
    show(
        "generate_regulatory_package (summary)",
        {
            "application_id": package.get("application_id"),
            "examination_date": package.get("examination_date"),
            "event_count": len(package.get("event_stream", [])),
            "integrity_verified": package.get("integrity_verification", {}).get("is_valid"),
            "narrative_lines": len(package.get("narrative", [])),
            "compliance_status": package.get("projection_states", {})
            .get("compliance_audit_view", {})
            .get("status"),
        },
    )

    # ── 9. Final projection state ──────────────────────────────────────────────
    section("9. FINAL APPLICATION SUMMARY (PROJECTION)")
    row = await pool.fetchrow(
        "SELECT * FROM projection_application_summary WHERE application_id = $1", APP_ID
    )
    if row:
        show("projection_application_summary", dict(row))
    else:
        print("  [NOTE] Projection not yet updated — daemon processes async. Run again in 1s.")

    # ── 10. What-If counterfactual ─────────────────────────────────────────────
    section("10. WHAT-IF: risk_tier LOW → HIGH (confidence 0.88 → 0.45)")
    from ledger.core.whatif import run_whatif

    # The real lifecycle used risk_tier=LOW, confidence=0.88 → APPROVE
    # Counterfactual: same agent, same session, but HIGH risk and confidence 0.45
    # Confidence 0.45 < 0.6 floor → recommendation forced to REFER regardless
    cf_payload = {
        "application_id": APP_ID,
        "agent_id": CREDIT_AGENT,
        "session_id": CREDIT_SESS,
        "model_version": "v1.0",
        "risk_tier": "HIGH",
        "confidence_score": 0.45,
        "recommended_limit_usd": 20000.0,
        "analysis_duration_ms": 300,
        "input_data_hash": "sha256-counterfactual",
        "reasoning": "High leverage, elevated default risk under stress scenario.",
        "risk_score_raw": 0.45,
        "regulatory_basis": "EU-AI-ACT-2025",
    }

    whatif = await run_whatif(
        application_id=APP_ID,
        store=store,
        branch_event_type="CreditAnalysisCompleted",
        counterfactual_payload=cf_payload,
    )

    show(
        "whatif_result",
        {
            "application_id": whatif.application_id,
            "branch_at": whatif.branch_event_type,
            "events_replayed": whatif.events_replayed,
            "events_skipped": whatif.events_skipped,
            "diverging_fields": whatif.divergence_events,
            "real_outcome": {
                "state": whatif.real_outcome["state"],
                "risk_tier": whatif.real_outcome["risk_tier"],
                "decision": whatif.real_outcome["decision"],
                "confidence_score": whatif.real_outcome["confidence_score"],
                "final_decision": whatif.real_outcome["final_decision"],
            },
            "counterfactual_outcome": {
                "state": whatif.counterfactual_outcome["state"],
                "risk_tier": whatif.counterfactual_outcome["risk_tier"],
                "decision": whatif.counterfactual_outcome["decision"],
                "confidence_score": whatif.counterfactual_outcome["confidence_score"],
                "final_decision": whatif.counterfactual_outcome["final_decision"],
            },
            "materially_different": whatif.real_outcome["state"]
            != whatif.counterfactual_outcome["state"],
        },
    )

    # ── 11. Full regulatory package (complete ledger) ──────────────────────────
    section("11. FULL REGULATORY PACKAGE — COMPLETE LEDGER")
    # Re-use the package already generated in step 8
    show(
        "event_stream (all events in order)",
        [
            {
                "pos": e["stream_position"],
                "type": e["event_type"],
                "recorded_at": e["recorded_at"],
            }
            for e in package.get("event_stream", [])
        ],
    )
    show(
        "narrative (one sentence per event)",
        [f"[{n['recorded_at'][:19]}] {n['sentence']}" for n in package.get("narrative", [])],
    )
    show("agent_attribution", package.get("agent_attribution", []))
    show("integrity_verification", package.get("integrity_verification", {}))
    show("package_checksum", {"sha256": package.get("package_checksum")})

    await pool.close()
    print(f"\n{'=' * 60}")
    print("  ALL STEPS COMPLETED SUCCESSFULLY")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
