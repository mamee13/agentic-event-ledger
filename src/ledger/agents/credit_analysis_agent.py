"""
ledger/agents/credit_analysis_agent.py
=======================================
CreditAnalysisAgent — reference LangGraph implementation.

LangGraph nodes:
  validate_inputs → open_credit_record → load_applicant_registry →
  load_extracted_facts → analyze_credit_risk → apply_policy_constraints → write_output

Input streams read:
  loan-{id}    → ApplicationSubmitted (applicant_id, amount, purpose)
  docpkg-{id}  → ExtractionCompleted, QualityAssessmentCompleted

Output events:
  credit-{id}: CreditRecordOpened, HistoricalProfileConsumed,
               ExtractedFactsConsumed, CreditAnalysisCompleted (or CreditAnalysisDeferred)
  loan-{id}:   FraudScreeningRequested
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from ledger.agents.base_agent import BaseApexAgent
from ledger.core.models import BaseEvent


class CreditState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    applicant_id: str | None
    requested_amount_usd: float | None
    loan_purpose: str | None
    company_profile: dict[str, Any] | None
    historical_financials: list[dict[str, Any]] | None
    compliance_flags: list[dict[str, Any]] | None
    loan_history: list[dict[str, Any]] | None
    extracted_facts: dict[str, Any] | None
    quality_flags: list[str] | None
    document_ids_consumed: list[str] | None
    credit_decision: dict[str, Any] | None
    policy_violations: list[str] | None
    errors: list[str]
    output_events: list[dict[str, Any]]
    next_agent: str | None


class CreditAnalysisAgent(BaseApexAgent):
    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        import os

        m = model or os.environ.get("CREDIT_AGENT_MODEL")
        super().__init__(agent_id, agent_type, store, registry, model=m)

    def build_graph(self) -> Any:
        g = StateGraph(CreditState)
        g.add_node("validate_inputs", self._node_validate_inputs)
        g.add_node("open_credit_record", self._node_open_credit_record)
        g.add_node("load_applicant_registry", self._node_load_registry)
        g.add_node("load_extracted_facts", self._node_load_facts)
        g.add_node("analyze_credit_risk", self._node_analyze)
        g.add_node("apply_policy_constraints", self._node_policy)
        g.add_node("write_output", self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs", "open_credit_record")
        g.add_edge("open_credit_record", "load_applicant_registry")
        g.add_edge("load_applicant_registry", "load_extracted_facts")
        g.add_edge("load_extracted_facts", "analyze_credit_risk")
        g.add_edge("analyze_credit_risk", "apply_policy_constraints")
        g.add_edge("apply_policy_constraints", "write_output")
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> dict[str, Any]:
        return CreditState(  # type: ignore[return-value]
            application_id=application_id,
            session_id=self.session_id or "",
            agent_id=self.agent_id,
            applicant_id=None,
            requested_amount_usd=None,
            loan_purpose=None,
            company_profile=None,
            historical_financials=None,
            compliance_flags=None,
            loan_history=None,
            extracted_facts=None,
            quality_flags=None,
            document_ids_consumed=None,
            credit_decision=None,
            policy_violations=None,
            errors=[],
            output_events=[],
            next_agent=None,
        )

    # ── NODE 1: VALIDATE INPUTS ───────────────────────────────────────────────

    async def _node_validate_inputs(self, state: CreditState) -> CreditState:
        t = time.time()
        app_id = state["application_id"]
        errors: list[str] = []

        # Load ApplicationSubmitted from loan stream to get applicant_id, amount, purpose
        applicant_id = None
        requested_amount_usd = None
        loan_purpose = None
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            for ev in events:
                if ev.event_type == "ApplicationSubmitted":
                    p = ev.payload
                    applicant_id = p.get("applicant_id")
                    requested_amount_usd = float(p.get("requested_amount_usd", 0))
                    loan_purpose = p.get("loan_purpose", "unknown")
                    break
        except Exception as e:
            errors.append(f"Failed to load loan stream: {e}")

        if not applicant_id:
            errors.append("ApplicationSubmitted event not found on loan stream")

        ms = int((time.time() - t) * 1000)
        if errors:
            await self._record_input_failed([], errors)
            raise ValueError(f"Input validation failed: {errors}")

        await self._record_input_validated(
            ["application_id", "applicant_id", "document_package_ready"], ms
        )
        await self._record_node_execution(
            "validate_inputs",
            ["application_id"],
            ["applicant_id", "requested_amount_usd", "loan_purpose"],
            ms,
        )
        return {
            **state,
            "applicant_id": applicant_id,
            "requested_amount_usd": requested_amount_usd,
            "loan_purpose": loan_purpose,
            "errors": errors,
        }

    # ── NODE 2: OPEN CREDIT RECORD ────────────────────────────────────────────

    async def _node_open_credit_record(self, state: CreditState) -> CreditState:
        t = time.time()
        app_id = state["application_id"]

        # Check if stream already exists
        ver = await self.store.stream_version(f"credit-{app_id}")
        if ver == -1:
            event = BaseEvent(
                event_type="CreditRecordOpened",
                payload={
                    "application_id": app_id,
                    "applicant_id": state["applicant_id"],
                    "opened_at": datetime.now().isoformat(),
                },
            )
            await self.store.append(
                stream_id=f"credit-{app_id}", events=[event], expected_version=-1
            )

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "open_credit_record",
            ["application_id", "applicant_id"],
            ["credit_record_opened"],
            ms,
        )
        return state

    # ── NODE 3: LOAD APPLICANT REGISTRY ──────────────────────────────────────

    async def _node_load_registry(self, state: CreditState) -> CreditState:
        t = time.time()
        applicant_id = state["applicant_id"]

        profile_obj = await self.registry.get_company(applicant_id)
        financials_obj = await self.registry.get_financial_history(applicant_id)
        flags_obj = await self.registry.get_compliance_flags(applicant_id)
        loans = await self.registry.get_loan_relationships(applicant_id)

        # Convert dataclasses to dicts for state
        profile = vars(profile_obj) if profile_obj else {}
        financials = [vars(f) for f in financials_obj]
        flags = [vars(f) for f in flags_obj]

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "query_applicant_registry",
            f"company_id={applicant_id}",
            f"Loaded profile, {len(financials)} fiscal years, "
            f"{len(flags)} flags, {len(loans)} loans",
            ms,
        )

        # Record HistoricalProfileConsumed
        has_defaults = any(loan.get("default_occurred") for loan in loans)
        traj = profile.get("trajectory", "UNKNOWN")
        consumed_event = BaseEvent(
            event_type="HistoricalProfileConsumed",
            payload={
                "application_id": state["application_id"],
                "session_id": self.session_id,
                "fiscal_years_loaded": [f["fiscal_year"] for f in financials],
                "has_prior_loans": bool(loans),
                "has_defaults": has_defaults,
                "revenue_trajectory": traj,
                "data_hash": self._sha({"fins": financials, "flags": flags}),
                "consumed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"credit-{state['application_id']}", [consumed_event])

        await self._record_node_execution(
            "load_applicant_registry",
            ["applicant_id"],
            ["company_profile", "historical_financials", "compliance_flags", "loan_history"],
            ms,
        )
        return {
            **state,
            "company_profile": profile,
            "historical_financials": financials,
            "compliance_flags": flags,
            "loan_history": loans,
        }

    # ── NODE 4: LOAD EXTRACTED FACTS ──────────────────────────────────────────

    async def _node_load_facts(self, state: CreditState) -> CreditState:
        t = time.time()
        app_id = state["application_id"]

        pkg_events = await self.store.load_stream(f"docpkg-{app_id}")
        extraction_events = [e for e in pkg_events if e.event_type == "ExtractionCompleted"]

        merged_facts: dict[str, Any] = {}
        doc_ids: list[str] = []
        quality_flags: list[str] = []

        for ev in extraction_events:
            payload = ev.payload
            doc_ids.append(payload.get("document_id", "unknown"))
            facts = payload.get("facts") or {}
            for k, v in facts.items():
                if v is not None and k not in merged_facts:
                    merged_facts[k] = v
            if facts.get("extraction_notes"):
                quality_flags.extend(facts["extraction_notes"])

        qa_events = [e for e in pkg_events if e.event_type == "QualityAssessmentCompleted"]
        for ev in qa_events:
            quality_flags.extend(ev.payload.get("anomalies", []))
            quality_flags.extend(
                [f"CRITICAL_MISSING:{f}" for f in ev.payload.get("critical_missing_fields", [])]
            )

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "load_event_store_stream",
            f"stream_id=docpkg-{app_id} filter=ExtractionCompleted",
            f"Loaded {len(extraction_events)} extraction results, {len(quality_flags)} flags",
            ms,
        )

        # Record ExtractedFactsConsumed
        consumed_event = BaseEvent(
            event_type="ExtractedFactsConsumed",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "document_ids_consumed": doc_ids,
                "facts_summary": (
                    f"revenue={merged_facts.get('total_revenue')}, "
                    f"net_income={merged_facts.get('net_income')}"
                ),
                "quality_flags_present": bool(quality_flags),
                "consumed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"credit-{app_id}", [consumed_event])

        # Defer if facts are too incomplete
        critical = ["total_revenue", "net_income", "total_assets"]
        missing_critical = [k for k in critical if not merged_facts.get(k)]
        if len(missing_critical) >= 2:
            defer_event = BaseEvent(
                event_type="CreditAnalysisDeferred",
                payload={
                    "application_id": app_id,
                    "session_id": self.session_id,
                    "deferral_reason": "Insufficient document extraction quality",
                    "quality_issues": [f"Missing critical field: {f}" for f in missing_critical],
                    "deferred_at": datetime.now().isoformat(),
                },
            )
            await self._append_with_retry(f"credit-{app_id}", [defer_event])
            raise ValueError(f"Credit analysis deferred: missing {missing_critical}")

        await self._record_node_execution(
            "load_extracted_facts",
            ["document_package_events"],
            ["extracted_facts", "quality_flags"],
            ms,
        )
        return {
            **state,
            "extracted_facts": merged_facts,
            "quality_flags": quality_flags,
            "document_ids_consumed": doc_ids,
        }

    # ── NODE 5: ANALYZE CREDIT RISK (LLM) ─────────────────────────────────────

    async def _node_analyze(self, state: CreditState) -> CreditState:
        t = time.time()
        hist = state.get("historical_financials") or []
        facts = state.get("extracted_facts") or {}
        flags = state.get("compliance_flags") or []
        loans = state.get("loan_history") or []
        profile = state.get("company_profile") or {}
        q_flags = state.get("quality_flags") or []

        fins_table = (
            "\n".join(
                [
                    f"FY{f['fiscal_year']}: Revenue=${f.get('total_revenue', 0):,.0f}"
                    f" EBITDA=${f.get('ebitda', 0):,.0f}"
                    f" Net=${f.get('net_income', 0):,.0f}"
                    f" D/E={f.get('debt_to_equity', 0):.2f}x"
                    for f in hist
                ]
            )
            or "No historical data in registry"
        )

        system_prompt = """You are a commercial credit analyst at Apex Financial Services.
Evaluate the loan application and produce a CreditDecision as a JSON object.

HARD POLICY RULES (enforce regardless of your reasoning):
1. Maximum loan-to-revenue ratio: 0.35  (cap recommended_limit_usd at annual_revenue * 0.35)
2. Any prior loan default → risk_tier must be HIGH
3. Any ACTIVE compliance flag severity=HIGH → confidence must be <= 0.50

Respond ONLY with this JSON (no preamble):
{
  "risk_tier": "LOW" | "MEDIUM" | "HIGH",
  "recommended_limit_usd": <integer>,
  "confidence": <float 0.0-1.0>,
  "rationale": "<3-5 sentences>",
  "key_concerns": ["<concern>"],
  "data_quality_caveats": ["<caveat>"],
  "policy_overrides_applied": ["<rule ID if triggered>"]
}"""

        user_prompt = f"""LOAN APPLICATION
Applicant: {profile.get("name", "Unknown")} ({profile.get("industry", "Unknown")},
{profile.get("legal_type", "Unknown")})
Jurisdiction: {profile.get("jurisdiction", "Unknown")}
Requested Amount: ${state.get("requested_amount_usd", 0):,.0f}
Loan Purpose: {state.get("loan_purpose", "Unknown")}

HISTORICAL FINANCIAL PROFILE:
{fins_table}

CURRENT YEAR FACTS (extracted from submitted documents):
{json.dumps({k: str(v) for k, v in facts.items() if v is not None}, indent=2)}

DOCUMENT QUALITY FLAGS: {json.dumps(q_flags) if q_flags else "None"}
COMPLIANCE FLAGS: {json.dumps(flags) if flags else "None"}
PRIOR LOAN HISTORY: {json.dumps(loans) if loans else "No prior loan history on record"}

Provide your analysis as JSON."""

        decision: dict[str, Any]
        ti = to = 0
        cost = 0.0
        try:
            content, ti, to, cost = await self._call_llm(
                system_prompt, user_prompt, max_tokens=1024
            )
            decision = self._parse_json(content)
        except Exception as exc:
            decision = {
                "risk_tier": "MEDIUM",
                "recommended_limit_usd": int((state.get("requested_amount_usd") or 0) * 0.75),
                "confidence": 0.45,
                "rationale": f"Automated analysis failed ({exc!s:.80}). Human review required.",
                "key_concerns": ["Automated analysis error — human review required"],
                "data_quality_caveats": [],
                "policy_overrides_applied": ["ANALYSIS_FALLBACK"],
            }

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "analyze_credit_risk",
            ["historical_financials", "extracted_facts", "company_profile"],
            ["credit_decision"],
            ms,
            ti,
            to,
            cost,
        )
        return {**state, "credit_decision": decision}

    # ── NODE 6: APPLY POLICY CONSTRAINTS (deterministic) ─────────────────────

    async def _node_policy(self, state: CreditState) -> CreditState:
        t = time.time()
        d = dict(state["credit_decision"] or {})
        hist = state.get("historical_financials") or []
        flags = state.get("compliance_flags") or []
        loans = state.get("loan_history") or []
        viols: list[str] = []

        # Policy 1: loan-to-revenue cap
        if hist:
            rev = hist[-1].get("total_revenue", 0)
            cap = int(rev * 0.35)
            if cap > 0 and d.get("recommended_limit_usd", 0) > cap:
                d["recommended_limit_usd"] = cap
                viols.append(f"POLICY_REV_CAP: limit capped at 35% of revenue (${cap:,.0f})")

        # Policy 2: prior default → HIGH
        if any(loan.get("default_occurred") for loan in loans) and d.get("risk_tier") != "HIGH":
            d["risk_tier"] = "HIGH"
            viols.append("POLICY_PRIOR_DEFAULT: risk_tier elevated to HIGH")

        # Policy 3: active HIGH flag → confidence cap
        if (
            any(f.get("severity") == "HIGH" and f.get("is_active") for f in flags)
            and d.get("confidence", 0) > 0.50
        ):
            d["confidence"] = 0.50
            viols.append("POLICY_COMPLIANCE_FLAG: confidence capped at 0.50")

        if viols:
            d["policy_overrides_applied"] = d.get("policy_overrides_applied", []) + viols

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "apply_policy_constraints",
            ["credit_decision", "historical_financials", "loan_history", "compliance_flags"],
            ["credit_decision"],
            ms,
        )
        return {**state, "credit_decision": d, "policy_violations": viols}

    # ── NODE 7: WRITE OUTPUT ──────────────────────────────────────────────────

    async def _node_write_output(self, state: CreditState) -> CreditState:
        t = time.time()
        app_id = state["application_id"]
        d: dict[str, Any] = state["credit_decision"] or {}

        credit_event = BaseEvent(
            event_type="CreditAnalysisCompleted",
            event_version=2,
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "agent_id": self.agent_id,
                "risk_tier": d.get("risk_tier", "MEDIUM"),
                "recommended_limit_usd": d.get("recommended_limit_usd", 0),
                "confidence": float(d.get("confidence", 0.5)),
                "rationale": d.get("rationale", ""),
                "key_concerns": d.get("key_concerns", []),
                "data_quality_caveats": d.get("data_quality_caveats", []),
                "policy_overrides_applied": d.get("policy_overrides_applied", []),
                "model_version": self.model,
                "input_data_hash": self._sha(state),
                "completed_at": datetime.now().isoformat(),
            },
        )
        positions = await self._append_with_retry(
            f"credit-{app_id}", [credit_event], causation_id=self.session_id
        )

        fraud_trigger = BaseEvent(
            event_type="FraudScreeningRequested",
            payload={
                "application_id": app_id,
                "requested_at": datetime.now().isoformat(),
                "triggered_by_session": self.session_id,
            },
        )
        await self._append_with_retry(f"loan-{app_id}", [fraud_trigger])

        events_written = [
            {
                "stream_id": f"credit-{app_id}",
                "event_type": "CreditAnalysisCompleted",
                "stream_position": positions[0] if positions else -1,
            },
            {
                "stream_id": f"loan-{app_id}",
                "event_type": "FraudScreeningRequested",
                "stream_position": -1,
            },
        ]
        await self._record_output_written(
            events_written,
            f"Credit: {d.get('risk_tier')} risk, "
            f"${d.get('recommended_limit_usd', 0):,.0f} limit, "
            f"{d.get('confidence', 0):.0%} confidence. Fraud screening triggered.",
        )
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "write_output", ["credit_decision"], ["events_written"], ms
        )
        return {**state, "output_events": events_written, "next_agent": "fraud_detection"}
