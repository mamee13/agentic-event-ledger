"""
ledger/agents/stub_agents.py
============================
DocumentProcessingAgent, FraudDetectionAgent, ComplianceAgent, DecisionOrchestratorAgent.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any, TypedDict, cast

from langgraph.graph import END, StateGraph

from ledger.agents.base_agent import BaseApexAgent
from ledger.core.models import BaseEvent

# ─── DOCUMENT PROCESSING AGENT ───────────────────────────────────────────────


class DocProcState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    document_ids: list[str] | None
    document_paths: list[str] | None
    extraction_results: list[dict[str, Any]] | None
    quality_assessment: dict[str, Any] | None
    errors: list[str]
    output_events: list[dict[str, Any]]
    next_agent: str | None


class DocumentProcessingAgent(BaseApexAgent):
    """Wraps the Week 3 Document Intelligence pipeline as a LangGraph agent."""

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        import os

        m = model or os.environ.get("DOC_AGENT_MODEL")
        super().__init__(agent_id, agent_type, store, registry, model=m)

    def build_graph(self) -> Any:
        g = StateGraph(DocProcState)
        g.add_node("validate_inputs", self._node_validate_inputs)
        g.add_node("validate_document_formats", self._node_validate_formats)
        g.add_node("extract_income_statement", self._node_extract_is)
        g.add_node("extract_balance_sheet", self._node_extract_bs)
        g.add_node("assess_quality", self._node_assess_quality)
        g.add_node("write_output", self._node_write_output)
        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs", "validate_document_formats")
        g.add_edge("validate_document_formats", "extract_income_statement")
        g.add_edge("extract_income_statement", "extract_balance_sheet")
        g.add_edge("extract_balance_sheet", "assess_quality")
        g.add_edge("assess_quality", "write_output")
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> dict[str, Any]:
        return DocProcState(  # type: ignore[return-value]
            application_id=application_id,
            session_id=self.session_id or "",
            agent_id=self.agent_id,
            document_ids=None,
            document_paths=None,
            extraction_results=None,
            quality_assessment=None,
            errors=[],
            output_events=[],
            next_agent=None,
        )

    async def _node_validate_inputs(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        doc_ids: list[str] = []
        doc_paths: list[str] = []
        errors: list[str] = []
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            for ev in events:
                if ev.event_type == "DocumentUploaded":
                    p = ev.payload
                    doc_ids.append(p.get("document_id", ""))
                    doc_paths.append(p.get("file_path", ""))
        except Exception as e:
            errors.append(f"Failed to load loan stream: {e}")

        ms = int((time.time() - t) * 1000)
        if errors:
            await self._record_input_failed([], errors)
            raise ValueError(f"Input validation failed: {errors}")
        await self._record_input_validated(["application_id", "document_ids"], ms)
        await self._record_node_execution(
            "validate_inputs", ["application_id"], ["document_ids", "document_paths"], ms
        )
        return {**state, "document_ids": doc_ids, "document_paths": doc_paths, "errors": errors}

    async def _node_validate_formats(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        doc_ids = state.get("document_ids") or []
        for doc_id in doc_ids:
            event = BaseEvent(
                event_type="DocumentFormatValidated",
                payload={
                    "package_id": app_id,
                    "document_id": doc_id,
                    "detected_format": "PDF",
                    "page_count": 1,
                    "validated_at": datetime.now().isoformat(),
                },
            )
            await self._append_with_retry(f"docpkg-{app_id}", [event])
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "validate_document_formats", ["document_ids"], ["format_validated"], ms
        )
        return state

    async def _node_extract_is(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        doc_ids = state.get("document_ids") or []
        doc_id = doc_ids[0] if doc_ids else "income_statement"
        results = list(state.get("extraction_results") or [])

        start_event = BaseEvent(
            event_type="ExtractionStarted",
            payload={
                "package_id": app_id,
                "document_id": doc_id,
                "pipeline_version": "mineru-1.0",
                "started_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"docpkg-{app_id}", [start_event])

        facts: dict[str, Any] = {}
        try:
            from document_refinery.pipeline import extract_financial_facts

            paths = state.get("document_paths") or []
            file_path = paths[0] if paths else ""
            facts = await extract_financial_facts(file_path, "income_statement")
        except Exception:
            facts = {}

        result = {"document_id": doc_id, "document_type": "income_statement", "facts": facts}
        results.append(result)

        done_event = BaseEvent(
            event_type="ExtractionCompleted",
            payload={
                "package_id": app_id,
                "document_id": doc_id,
                "facts": facts,
                "completed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"docpkg-{app_id}", [done_event])

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "week3_extraction_pipeline",
            f"doc={doc_id} type=income_statement",
            f"facts={list(facts.keys())}",
            ms,
        )
        await self._record_node_execution(
            "extract_income_statement", ["document_paths"], ["extraction_results"], ms
        )
        return {**state, "extraction_results": results}

    async def _node_extract_bs(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        doc_ids = state.get("document_ids") or []
        doc_id = doc_ids[1] if len(doc_ids) > 1 else "balance_sheet"
        results = list(state.get("extraction_results") or [])

        start_event = BaseEvent(
            event_type="ExtractionStarted",
            payload={
                "package_id": app_id,
                "document_id": doc_id,
                "pipeline_version": "mineru-1.0",
                "started_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"docpkg-{app_id}", [start_event])

        facts: dict[str, Any] = {}
        try:
            from document_refinery.pipeline import extract_financial_facts

            paths = state.get("document_paths") or []
            file_path = paths[1] if len(paths) > 1 else ""
            facts = await extract_financial_facts(file_path, "balance_sheet")
        except Exception:
            facts = {}

        result = {"document_id": doc_id, "document_type": "balance_sheet", "facts": facts}
        results.append(result)

        done_event = BaseEvent(
            event_type="ExtractionCompleted",
            payload={
                "package_id": app_id,
                "document_id": doc_id,
                "facts": facts,
                "completed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"docpkg-{app_id}", [done_event])

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "week3_extraction_pipeline",
            f"doc={doc_id} type=balance_sheet",
            f"facts={list(facts.keys())}",
            ms,
        )
        await self._record_node_execution(
            "extract_balance_sheet", ["document_paths"], ["extraction_results"], ms
        )
        return {**state, "extraction_results": results}

    async def _node_assess_quality(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]
        results = state.get("extraction_results") or []
        merged: dict[str, Any] = {}
        for r in results:
            for k, v in (r.get("facts") or {}).items():
                if v is not None and k not in merged:
                    merged[k] = v

        system_prompt = (
            "You are a financial document quality analyst. "
            "Check internal consistency. Do NOT make credit decisions. "
            "Return DocumentQualityAssessment JSON with fields: "
            "is_consistent (bool), anomalies (list[str]), "
            "critical_missing_fields (list[str]), overall_quality (LOW|MEDIUM|HIGH)."
        )
        facts_json = json.dumps({k: str(v) for k, v in merged.items()}, indent=2)
        user_prompt = f"Extracted financial facts:\n{facts_json}"

        assessment: dict[str, Any] = {
            "is_consistent": True,
            "anomalies": [],
            "critical_missing_fields": [],
            "overall_quality": "MEDIUM",
        }
        ti = to = 0
        cost = 0.0
        try:
            content, ti, to, cost = await self._call_llm(system_prompt, user_prompt, max_tokens=512)
            assessment = self._parse_json(content)
        except Exception:
            pass

        qa_event = BaseEvent(
            event_type="QualityAssessmentCompleted",
            payload={
                "package_id": app_id,
                "is_consistent": assessment.get("is_consistent", True),
                "anomalies": assessment.get("anomalies", []),
                "critical_missing_fields": assessment.get("critical_missing_fields", []),
                "overall_quality": assessment.get("overall_quality", "MEDIUM"),
                "assessed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"docpkg-{app_id}", [qa_event])

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "assess_quality", ["extraction_results"], ["quality_assessment"], ms, ti, to, cost
        )
        return {**state, "quality_assessment": assessment}

    async def _node_write_output(self, state: DocProcState) -> DocProcState:
        t = time.time()
        app_id = state["application_id"]

        ready_event = BaseEvent(
            event_type="PackageReadyForAnalysis",
            payload={"package_id": app_id, "ready_at": datetime.now().isoformat()},
        )
        await self._append_with_retry(f"docpkg-{app_id}", [ready_event])

        trigger_event = BaseEvent(
            event_type="CreditAnalysisRequested",
            payload={"application_id": app_id, "requested_at": datetime.now().isoformat()},
        )
        await self._append_with_retry(f"loan-{app_id}", [trigger_event])

        events_written = [
            {"stream_id": f"docpkg-{app_id}", "event_type": "PackageReadyForAnalysis"},
            {"stream_id": f"loan-{app_id}", "event_type": "CreditAnalysisRequested"},
        ]
        await self._record_output_written(
            events_written, "Document package ready. Credit analysis triggered."
        )
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "write_output", ["quality_assessment"], ["events_written"], ms
        )
        return {**state, "output_events": events_written, "next_agent": "credit_analysis"}


# ─── FRAUD DETECTION AGENT ───────────────────────────────────────────────────


class FraudState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    extracted_facts: dict[str, Any] | None
    registry_profile: dict[str, Any] | None
    historical_financials: list[dict[str, Any]] | None
    fraud_signals: list[dict[str, Any]] | None
    fraud_score: float | None
    anomalies: list[dict[str, Any]] | None
    errors: list[str]
    output_events: list[dict[str, Any]]
    next_agent: str | None


class FraudDetectionAgent(BaseApexAgent):
    """Cross-references extracted document facts against historical registry data."""

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        import os

        m = model or os.environ.get("FRAUD_AGENT_MODEL")
        super().__init__(agent_id, agent_type, store, registry, model=m)

    def build_graph(self) -> Any:
        g = StateGraph(FraudState)
        g.add_node("validate_inputs", self._node_validate_inputs)
        g.add_node("load_document_facts", self._node_load_facts)
        g.add_node("cross_reference_registry", self._node_cross_reference)
        g.add_node("analyze_fraud_patterns", self._node_analyze)
        g.add_node("write_output", self._node_write_output)
        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs", "load_document_facts")
        g.add_edge("load_document_facts", "cross_reference_registry")
        g.add_edge("cross_reference_registry", "analyze_fraud_patterns")
        g.add_edge("analyze_fraud_patterns", "write_output")
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> Any:
        return FraudState(
            application_id=application_id,
            session_id=self.session_id or "",
            agent_id=self.agent_id,
            extracted_facts=None,
            registry_profile=None,
            historical_financials=None,
            fraud_signals=None,
            fraud_score=None,
            anomalies=None,
            errors=[],
            output_events=[],
            next_agent=None,
        )

    async def _node_validate_inputs(self, state: FraudState) -> FraudState:
        t = time.time()
        app_id = state["application_id"]
        errors: list[str] = []
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            has_trigger = any(e.event_type == "FraudScreeningRequested" for e in events)
            if not has_trigger:
                errors.append("FraudScreeningRequested event not found on loan stream")
        except Exception as e:
            errors.append(f"Failed to load loan stream: {e}")
        ms = int((time.time() - t) * 1000)
        if errors:
            await self._record_input_failed([], errors)
            raise ValueError(f"Input validation failed: {errors}")
        await self._record_input_validated(["application_id", "fraud_trigger"], ms)
        await self._record_node_execution(
            "validate_inputs", ["application_id"], ["trigger_verified"], ms
        )
        return {**state, "errors": errors}

    async def _node_load_facts(self, state: FraudState) -> FraudState:
        t = time.time()
        app_id = state["application_id"]
        merged: dict[str, Any] = {}
        try:
            pkg_events = await self.store.load_stream(f"docpkg-{app_id}")
            for ev in pkg_events:
                if ev.event_type == "ExtractionCompleted":
                    for k, v in (ev.payload.get("facts") or {}).items():
                        if v is not None and k not in merged:
                            merged[k] = v
        except Exception:
            pass
        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "load_event_store_stream", f"docpkg-{app_id}", f"facts={list(merged.keys())}", ms
        )
        await self._record_node_execution(
            "load_document_facts", ["docpkg_stream"], ["extracted_facts"], ms
        )
        return {**state, "extracted_facts": merged}

    async def _node_cross_reference(self, state: FraudState) -> FraudState:
        t = time.time()
        app_id = state["application_id"]
        applicant_id = None
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            for ev in events:
                if ev.event_type == "ApplicationSubmitted":
                    applicant_id = ev.payload.get("applicant_id")
                    break
        except Exception:
            pass

        profile: dict[str, Any] = {}
        financials: list[dict[str, Any]] = []
        if applicant_id and self.registry:
            try:
                p = await self.registry.get_company(applicant_id)
                profile = vars(p) if p else {}
                fh = await self.registry.get_financial_history(applicant_id)
                financials = [vars(f) for f in fh]
            except Exception:
                pass

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "query_applicant_registry",
            f"company_id={applicant_id}",
            f"{len(financials)} fiscal years",
            ms,
        )
        await self._record_node_execution(
            "cross_reference_registry",
            ["applicant_id"],
            ["registry_profile", "historical_financials"],
            ms,
        )
        return {**state, "registry_profile": profile, "historical_financials": financials}

    async def _node_analyze(self, state: FraudState) -> FraudState:
        t = time.time()
        facts = state.get("extracted_facts") or {}
        hist = state.get("historical_financials") or []
        profile = state.get("registry_profile") or {}

        # Deterministic base scoring
        fraud_score = 0.05
        signals: list[dict[str, Any]] = []

        if hist and facts.get("total_revenue"):
            prior_rev = hist[-1].get("total_revenue", 0)
            doc_rev = float(facts.get("total_revenue", 0))
            if prior_rev > 0:
                gap = abs(doc_rev - prior_rev) / prior_rev
                traj = profile.get("trajectory", "STABLE")
                if gap > 0.40 and traj not in ("GROWTH", "RECOVERING"):
                    fraud_score += 0.25
                    signals.append(
                        {
                            "type": "REVENUE_DISCREPANCY",
                            "severity": "HIGH",
                            "gap_pct": round(gap, 3),
                        }
                    )

        total_assets = float(facts.get("total_assets", 0))
        total_liab = float(facts.get("total_liabilities", 0))
        total_eq = float(facts.get("total_equity", 0))
        if total_assets > 0 and (total_liab + total_eq) > 0:
            bs_gap = abs(total_assets - (total_liab + total_eq)) / total_assets
            if bs_gap > 0.05:
                fraud_score += 0.20
                signals.append(
                    {
                        "type": "BALANCE_SHEET_INCONSISTENCY",
                        "severity": "MEDIUM",
                        "gap_pct": round(bs_gap, 3),
                    }
                )

        # LLM for named anomaly identification
        system_prompt = (
            "You are a financial fraud analyst. "
            "Given cross-reference results, identify specific named anomalies. "
            "For each anomaly: type, severity (LOW/MEDIUM/HIGH), evidence, affected_fields. "
            "Compute a final fraud_score 0-1. "
            "Return FraudAssessment JSON: "
            "{fraud_score, anomalies: [], recommendation: PROCEED|FLAG_FOR_REVIEW|DECLINE}"
        )
        user_prompt = (
            f"Extracted facts: {json.dumps({k: str(v) for k, v in facts.items()}, indent=2)}\n"
            f"Registry history (last year): {json.dumps(hist[-1] if hist else {}, default=str)}\n"
            f"Pre-computed signals: {json.dumps(signals)}\n"
            f"Base fraud_score: {fraud_score}"
        )
        anomalies: list[dict[str, Any]] = signals
        ti = to = 0
        cost = 0.0
        try:
            content, ti, to, cost = await self._call_llm(system_prompt, user_prompt, max_tokens=512)
            result = self._parse_json(content)
            fraud_score = float(result.get("fraud_score", fraud_score))
            anomalies = result.get("anomalies", signals)
        except Exception:
            pass

        fraud_score = max(0.0, min(1.0, fraud_score))
        if fraud_score > 0.60:
            recommendation = "DECLINE"
        elif fraud_score >= 0.30:
            recommendation = "FLAG_FOR_REVIEW"
        else:
            recommendation = "PROCEED"

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "analyze_fraud_patterns",
            ["extracted_facts", "historical_financials"],
            ["fraud_score", "anomalies"],
            ms,
            ti,
            to,
            cost,
        )
        return {
            **state,
            "fraud_score": fraud_score,
            "anomalies": anomalies,
            "fraud_signals": [{"recommendation": recommendation}],
        }

    async def _node_write_output(self, state: FraudState) -> FraudState:
        t = time.time()
        app_id = state["application_id"]
        fraud_score = state.get("fraud_score", 0.05)
        anomalies = state.get("anomalies") or []
        signals = state.get("fraud_signals") or []
        recommendation = signals[0].get("recommendation", "PROCEED") if signals else "PROCEED"

        # Append FraudScreeningInitiated
        init_event = BaseEvent(
            event_type="FraudScreeningInitiated",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "initiated_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"fraud-{app_id}", [init_event])

        # Append FraudAnomalyDetected for each MEDIUM+ anomaly
        for anomaly in anomalies:
            if anomaly.get("severity") in ("MEDIUM", "HIGH"):
                anom_event = BaseEvent(
                    event_type="FraudAnomalyDetected",
                    payload={
                        "application_id": app_id,
                        "anomaly_type": anomaly.get("type", "UNKNOWN"),
                        "severity": anomaly.get("severity", "MEDIUM"),
                        "evidence": anomaly.get("evidence", ""),
                        "affected_fields": anomaly.get("affected_fields", []),
                        "detected_at": datetime.now().isoformat(),
                    },
                )
                await self._append_with_retry(f"fraud-{app_id}", [anom_event])

        # Append FraudScreeningCompleted
        done_event = BaseEvent(
            event_type="FraudScreeningCompleted",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "fraud_score": fraud_score,
                "anomalies": anomalies,
                "recommendation": recommendation,
                "completed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"fraud-{app_id}", [done_event])

        # Trigger next agent
        trigger = BaseEvent(
            event_type="ComplianceCheckRequested",
            payload={"application_id": app_id, "requested_at": datetime.now().isoformat()},
        )
        await self._append_with_retry(f"loan-{app_id}", [trigger])

        events_written = [
            {"stream_id": f"fraud-{app_id}", "event_type": "FraudScreeningCompleted"},
            {"stream_id": f"loan-{app_id}", "event_type": "ComplianceCheckRequested"},
        ]
        await self._record_output_written(
            events_written, f"Fraud score: {fraud_score:.2f}, recommendation: {recommendation}"
        )
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "write_output", ["fraud_score", "anomalies"], ["events_written"], ms
        )
        return cast(
            FraudState,
            {**state, "output_events": events_written, "next_agent": "compliance"},
        )


# ─── COMPLIANCE AGENT ─────────────────────────────────────────────────────────


class ComplianceState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    company_profile: dict[str, Any] | None
    requested_amount_usd: float | None
    rule_results: list[dict[str, Any]] | None
    has_hard_block: bool
    block_rule_id: str | None
    errors: list[str]
    output_events: list[dict[str, Any]]
    next_agent: str | None


# Regulation definitions — deterministic, no LLM in decision path
REGULATIONS: dict[str, Any] = {
    "REG-001": {
        "name": "Bank Secrecy Act (BSA) Check",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda co: (
            not any(
                f.get("flag_type") == "AML_WATCH" and f.get("is_active")
                for f in co.get("compliance_flags", [])
            )
        ),
        "failure_reason": "Active AML Watch flag present. Remediation required.",
        "remediation": "Provide enhanced due diligence documentation within 10 business days.",
    },
    "REG-002": {
        "name": "OFAC Sanctions Screening",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: (
            not any(
                f.get("flag_type") == "SANCTIONS_REVIEW" and f.get("is_active")
                for f in co.get("compliance_flags", [])
            )
        ),
        "failure_reason": "Active OFAC Sanctions Review. Application blocked.",
        "remediation": None,
    },
    "REG-003": {
        "name": "Jurisdiction Lending Eligibility",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: co.get("jurisdiction") != "MT",
        "failure_reason": "Jurisdiction MT not approved for commercial lending at this time.",
        "remediation": None,
    },
    "REG-004": {
        "name": "Legal Entity Type Eligibility",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda co: (
            not (
                co.get("legal_type") == "Sole Proprietor"
                and (co.get("requested_amount_usd", 0) or 0) > 250_000
            )
        ),
        "failure_reason": "Sole Proprietor loans >$250K require additional documentation.",
        "remediation": "Submit SBA Form 912 and personal financial statement.",
    },
    "REG-005": {
        "name": "Minimum Operating History",
        "version": "2026-Q1-v1",
        "is_hard_block": True,
        "check": lambda co: (
            (datetime.now().year - (co.get("founded_year") or datetime.now().year)) >= 2
        ),
        "failure_reason": "Business must have at least 2 years of operating history.",
        "remediation": None,
    },
    "REG-006": {
        "name": "CRA Community Reinvestment",
        "version": "2026-Q1-v1",
        "is_hard_block": False,
        "check": lambda _co: True,
        "note_type": "CRA_CONSIDERATION",
        "note_text": "Jurisdiction qualifies for Community Reinvestment Act consideration.",
    },
}


class ComplianceAgent(BaseApexAgent):
    """Evaluates 6 deterministic regulatory rules. No LLM in decision path."""

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        import os

        m = model or os.environ.get("COMPLIANCE_AGENT_MODEL")
        super().__init__(agent_id, agent_type, store, registry, model=m)

    def build_graph(self) -> Any:
        g = StateGraph(ComplianceState)
        g.add_node("validate_inputs", self._node_validate_inputs)
        g.add_node("load_company_profile", self._node_load_profile)

        async def _reg001(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-001")

        async def _reg002(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-002")

        async def _reg003(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-003")

        async def _reg004(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-004")

        async def _reg005(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-005")

        async def _reg006(s: ComplianceState) -> Any:
            return await self._evaluate_rule(s, "REG-006")

        g.add_node("evaluate_reg001", cast(Any, _reg001))
        g.add_node("evaluate_reg002", cast(Any, _reg002))
        g.add_node("evaluate_reg003", cast(Any, _reg003))
        g.add_node("evaluate_reg004", cast(Any, _reg004))
        g.add_node("evaluate_reg005", cast(Any, _reg005))
        g.add_node("evaluate_reg006", cast(Any, _reg006))
        g.add_node("write_output", self._node_write_output)

        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs", "load_company_profile")
        g.add_edge("load_company_profile", "evaluate_reg001")

        for src, nxt in [
            ("evaluate_reg001", "evaluate_reg002"),
            ("evaluate_reg002", "evaluate_reg003"),
            ("evaluate_reg003", "evaluate_reg004"),
            ("evaluate_reg004", "evaluate_reg005"),
            ("evaluate_reg005", "evaluate_reg006"),
            ("evaluate_reg006", "write_output"),
        ]:
            g.add_conditional_edges(
                src,
                lambda s, _nxt=nxt: "write_output" if s["has_hard_block"] else _nxt,
            )
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> Any:
        return ComplianceState(
            application_id=application_id,
            session_id=self.session_id or "",
            agent_id=self.agent_id,
            company_profile=None,
            requested_amount_usd=None,
            rule_results=[],
            has_hard_block=False,
            block_rule_id=None,
            errors=[],
            output_events=[],
            next_agent=None,
        )

    async def _node_validate_inputs(self, state: ComplianceState) -> ComplianceState:
        t = time.time()
        app_id = state["application_id"]
        errors: list[str] = []
        requested_amount_usd = None
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            has_trigger = any(e.event_type == "ComplianceCheckRequested" for e in events)
            if not has_trigger:
                errors.append("ComplianceCheckRequested event not found on loan stream")
            for ev in events:
                if ev.event_type == "ApplicationSubmitted":
                    requested_amount_usd = float(ev.payload.get("requested_amount_usd", 0))
                    break
        except Exception as e:
            errors.append(f"Failed to load loan stream: {e}")
        ms = int((time.time() - t) * 1000)
        if errors:
            await self._record_input_failed([], errors)
            raise ValueError(f"Input validation failed: {errors}")
        await self._record_input_validated(["application_id", "compliance_trigger"], ms)
        await self._record_node_execution(
            "validate_inputs", ["application_id"], ["trigger_verified"], ms
        )
        return {**state, "requested_amount_usd": requested_amount_usd, "errors": errors}

    async def _node_load_profile(self, state: ComplianceState) -> ComplianceState:
        t = time.time()
        app_id = state["application_id"]
        applicant_id = None
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            for ev in events:
                if ev.event_type == "ApplicationSubmitted":
                    applicant_id = ev.payload.get("applicant_id")
                    break
        except Exception:
            pass

        profile: dict[str, Any] = {}
        if applicant_id and self.registry:
            try:
                p = await self.registry.get_company(applicant_id)
                profile = vars(p) if p else {}
                flags = await self.registry.get_compliance_flags(applicant_id)
                profile["compliance_flags"] = [vars(f) for f in flags]
            except Exception:
                pass

        # Inject requested_amount_usd so REG-004 lambda can access it
        profile["requested_amount_usd"] = state.get("requested_amount_usd", 0)

        # Append ComplianceCheckInitiated
        init_event = BaseEvent(
            event_type="ComplianceCheckInitiated",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "initiated_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"compliance-{app_id}", [init_event])

        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "query_applicant_registry",
            f"company_id={applicant_id}",
            "profile + compliance flags loaded",
            ms,
        )
        await self._record_node_execution(
            "load_company_profile", ["applicant_id"], ["company_profile"], ms
        )
        return {**state, "company_profile": profile}

    async def _evaluate_rule(self, state: ComplianceState, rule_id: str) -> ComplianceState:
        t = time.time()
        app_id = state["application_id"]
        reg = REGULATIONS[rule_id]
        co = state.get("company_profile") or {}
        passes = reg["check"](co)
        evidence_hash = self._sha(f"{rule_id}-{co.get('company_id', '')}-{passes}")
        rule_results = list(state.get("rule_results") or [])

        if rule_id == "REG-006":
            event = BaseEvent(
                event_type="ComplianceRuleNoted",
                payload={
                    "application_id": app_id,
                    "rule_id": rule_id,
                    "rule_name": reg["name"],
                    "rule_version": reg["version"],
                    "note_type": reg.get("note_type", ""),
                    "note_text": reg.get("note_text", ""),
                    "evidence_hash": evidence_hash,
                    "evaluated_at": datetime.now().isoformat(),
                },
            )
            rule_results.append({"rule_id": rule_id, "status": "NOTED"})
        elif passes:
            event = BaseEvent(
                event_type="ComplianceRulePassed",
                payload={
                    "application_id": app_id,
                    "rule_id": rule_id,
                    "rule_name": reg["name"],
                    "rule_version": reg["version"],
                    "evidence_hash": evidence_hash,
                    "evaluated_at": datetime.now().isoformat(),
                },
            )
            rule_results.append({"rule_id": rule_id, "status": "PASSED"})
        else:
            event = BaseEvent(
                event_type="ComplianceRuleFailed",
                payload={
                    "application_id": app_id,
                    "rule_id": rule_id,
                    "rule_name": reg["name"],
                    "rule_version": reg["version"],
                    "is_hard_block": reg["is_hard_block"],
                    "failure_reason": reg["failure_reason"],
                    "remediation": reg.get("remediation"),
                    "evidence_hash": evidence_hash,
                    "evaluated_at": datetime.now().isoformat(),
                },
            )
            rule_results.append(
                {"rule_id": rule_id, "status": "FAILED", "is_hard_block": reg["is_hard_block"]}
            )

        await self._append_with_retry(f"compliance-{app_id}", [event])

        node_name = f"evaluate_{rule_id.lower().replace('-', '_')}"
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(node_name, ["company_profile"], [f"{rule_id}_result"], ms)

        new_state = {**state, "rule_results": rule_results}
        if not passes and reg["is_hard_block"]:
            new_state["has_hard_block"] = True
            new_state["block_rule_id"] = rule_id
        return cast(ComplianceState, new_state)

    async def _node_write_output(self, state: ComplianceState) -> ComplianceState:
        t = time.time()
        app_id = state["application_id"]
        has_block = state.get("has_hard_block", False)
        block_rule = state.get("block_rule_id")
        rule_results = state.get("rule_results") or []

        failed_rules = [r["rule_id"] for r in rule_results if r.get("status") == "FAILED"]
        overall_verdict = "BLOCKED" if has_block else ("CONDITIONAL" if failed_rules else "CLEAR")

        done_event = BaseEvent(
            event_type="ComplianceCheckCompleted",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "overall_verdict": overall_verdict,
                "rules_evaluated": len(rule_results),
                "failed_rules": failed_rules,
                "block_rule_id": block_rule,
                "completed_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"compliance-{app_id}", [done_event])

        events_written = [
            {"stream_id": f"compliance-{app_id}", "event_type": "ComplianceCheckCompleted"}
        ]

        if has_block:
            decline_event = BaseEvent(
                event_type="ApplicationDeclined",
                payload={
                    "application_id": app_id,
                    "reason": f"Compliance hard block: {block_rule}",
                    "adverse_action_notice_required": True,
                    "declined_at": datetime.now().isoformat(),
                },
            )
            await self._append_with_retry(f"loan-{app_id}", [decline_event])
            events_written.append(
                {"stream_id": f"loan-{app_id}", "event_type": "ApplicationDeclined"}
            )
            next_agent = None
        else:
            decision_event = BaseEvent(
                event_type="DecisionRequested",
                payload={"application_id": app_id, "requested_at": datetime.now().isoformat()},
            )
            await self._append_with_retry(f"loan-{app_id}", [decision_event])
            events_written.append(
                {"stream_id": f"loan-{app_id}", "event_type": "DecisionRequested"}
            )
            next_agent = "decision_orchestrator"

        await self._record_output_written(events_written, f"Compliance verdict: {overall_verdict}")
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution("write_output", ["rule_results"], ["events_written"], ms)
        return cast(
            ComplianceState,
            {**state, "output_events": events_written, "next_agent": next_agent},
        )


# ─── DECISION ORCHESTRATOR ────────────────────────────────────────────────────


class OrchestratorState(TypedDict):
    application_id: str
    session_id: str
    agent_id: str
    credit_result: dict[str, Any] | None
    fraud_result: dict[str, Any] | None
    compliance_result: dict[str, Any] | None
    recommendation: str | None
    confidence: float | None
    approved_amount: float | None
    executive_summary: str | None
    conditions: list[str] | None
    hard_constraints_applied: list[str] | None
    errors: list[str]
    output_events: list[dict[str, Any]]
    next_agent: str | None


class DecisionOrchestratorAgent(BaseApexAgent):
    """Synthesises all prior agent outputs into a final recommendation."""

    def __init__(
        self,
        agent_id: str,
        agent_type: str,
        store: Any,
        registry: Any,
        model: str | None = None,
    ):
        import os

        m = model or os.environ.get("DECISION_AGENT_MODEL")
        super().__init__(agent_id, agent_type, store, registry, model=m)

    def build_graph(self) -> Any:
        g = StateGraph(OrchestratorState)
        g.add_node("validate_inputs", self._node_validate_inputs)
        g.add_node("load_credit_result", self._node_load_credit)
        g.add_node("load_fraud_result", self._node_load_fraud)
        g.add_node("load_compliance_result", self._node_load_compliance)
        g.add_node("synthesize_decision", self._node_synthesize)
        g.add_node("apply_hard_constraints", self._node_constraints)
        g.add_node("write_output", self._node_write_output)
        g.set_entry_point("validate_inputs")
        g.add_edge("validate_inputs", "load_credit_result")
        g.add_edge("load_credit_result", "load_fraud_result")
        g.add_edge("load_fraud_result", "load_compliance_result")
        g.add_edge("load_compliance_result", "synthesize_decision")
        g.add_edge("synthesize_decision", "apply_hard_constraints")
        g.add_edge("apply_hard_constraints", "write_output")
        g.add_edge("write_output", END)
        return g.compile()

    def _initial_state(self, application_id: str) -> Any:
        return OrchestratorState(
            application_id=application_id,
            session_id=self.session_id or "",
            agent_id=self.agent_id,
            credit_result=None,
            fraud_result=None,
            compliance_result=None,
            recommendation=None,
            confidence=None,
            approved_amount=None,
            executive_summary=None,
            conditions=None,
            hard_constraints_applied=[],
            errors=[],
            output_events=[],
            next_agent=None,
        )

    async def _node_validate_inputs(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        app_id = state["application_id"]
        errors: list[str] = []
        try:
            events = await self.store.load_stream(f"loan-{app_id}")
            has_trigger = any(e.event_type == "DecisionRequested" for e in events)
            if not has_trigger:
                errors.append("DecisionRequested event not found on loan stream")
        except Exception as e:
            errors.append(f"Failed to load loan stream: {e}")
        ms = int((time.time() - t) * 1000)
        if errors:
            await self._record_input_failed([], errors)
            raise ValueError(f"Input validation failed: {errors}")
        await self._record_input_validated(["application_id", "decision_trigger"], ms)
        await self._record_node_execution(
            "validate_inputs", ["application_id"], ["trigger_verified"], ms
        )
        return {**state, "errors": errors}

    async def _node_load_credit(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        app_id = state["application_id"]
        credit_result: dict[str, Any] = {}
        try:
            events = await self.store.load_stream(f"credit-{app_id}")
            for ev in reversed(events):
                if ev.event_type == "CreditAnalysisCompleted":
                    credit_result = ev.payload
                    break
        except Exception:
            pass
        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "load_event_store_stream", f"credit-{app_id}", "CreditAnalysisCompleted loaded", ms
        )
        await self._record_node_execution(
            "load_credit_result", ["credit_stream"], ["credit_result"], ms
        )
        return {**state, "credit_result": credit_result}

    async def _node_load_fraud(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        app_id = state["application_id"]
        fraud_result: dict[str, Any] = {}
        try:
            events = await self.store.load_stream(f"fraud-{app_id}")
            for ev in reversed(events):
                if ev.event_type == "FraudScreeningCompleted":
                    fraud_result = ev.payload
                    break
        except Exception:
            pass
        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "load_event_store_stream", f"fraud-{app_id}", "FraudScreeningCompleted loaded", ms
        )
        await self._record_node_execution(
            "load_fraud_result", ["fraud_stream"], ["fraud_result"], ms
        )
        return {**state, "fraud_result": fraud_result}

    async def _node_load_compliance(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        app_id = state["application_id"]
        compliance_result: dict[str, Any] = {}
        try:
            events = await self.store.load_stream(f"compliance-{app_id}")
            for ev in reversed(events):
                if ev.event_type == "ComplianceCheckCompleted":
                    compliance_result = ev.payload
                    break
        except Exception:
            pass
        ms = int((time.time() - t) * 1000)
        await self._record_tool_call(
            "load_event_store_stream", f"compliance-{app_id}", "ComplianceCheckCompleted loaded", ms
        )
        await self._record_node_execution(
            "load_compliance_result", ["compliance_stream"], ["compliance_result"], ms
        )
        return {**state, "compliance_result": compliance_result}

    async def _node_synthesize(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        credit = state.get("credit_result") or {}
        fraud = state.get("fraud_result") or {}
        compliance = state.get("compliance_result") or {}

        system_prompt = (
            "You are a senior loan officer synthesising multi-agent analysis. "
            "Produce a recommendation (APPROVE/DECLINE/REFER), "
            "approved_amount_usd (integer), executive_summary (3-5 sentences), "
            "and key_risks (list of strings). "
            "Return OrchestratorDecision JSON only."
        )
        user_prompt = (
            f"Credit analysis: risk_tier={credit.get('risk_tier')}, "
            f"confidence={credit.get('confidence')}, "
            f"recommended_limit=${credit.get('recommended_limit_usd', 0):,.0f}\n"
            f"Fraud screening: fraud_score={fraud.get('fraud_score')}, "
            f"recommendation={fraud.get('recommendation')}\n"
            f"Compliance: overall_verdict={compliance.get('overall_verdict')}, "
            f"failed_rules={compliance.get('failed_rules', [])}\n"
            "Provide your synthesis as JSON."
        )

        recommendation = "REFER"
        approved_amount = float(credit.get("recommended_limit_usd", 0))
        executive_summary = "Multi-agent analysis complete."
        conditions: list[str] = []
        ti = to = 0
        cost = 0.0
        try:
            content, ti, to, cost = await self._call_llm(system_prompt, user_prompt, max_tokens=512)
            result = self._parse_json(content)
            recommendation = result.get("recommendation", "REFER")
            approved_amount = float(result.get("approved_amount_usd", approved_amount))
            executive_summary = result.get("executive_summary", executive_summary)
            conditions = result.get("key_risks", [])
        except Exception:
            pass

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "synthesize_decision",
            ["credit_result", "fraud_result", "compliance_result"],
            ["recommendation", "executive_summary"],
            ms,
            ti,
            to,
            cost,
        )
        return {
            **state,
            "recommendation": recommendation,
            "confidence": float(credit.get("confidence", 0.5)),
            "approved_amount": approved_amount,
            "executive_summary": executive_summary,
            "conditions": conditions,
        }

    async def _node_constraints(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        recommendation = state.get("recommendation", "REFER")
        confidence = float(state.get("confidence") or 0.5)
        fraud_score = float((state.get("fraud_result") or {}).get("fraud_score", 0.0))
        risk_tier = (state.get("credit_result") or {}).get("risk_tier", "MEDIUM")
        compliance_verdict = (state.get("compliance_result") or {}).get("overall_verdict", "CLEAR")
        applied: list[str] = list(state.get("hard_constraints_applied") or [])

        # Hard constraints — applied in order, cannot be overridden
        if compliance_verdict == "BLOCKED":
            recommendation = "DECLINE"
            applied.append("COMPLIANCE_BLOCKED")

        if confidence < 0.60:
            recommendation = "REFER"
            applied.append("CONFIDENCE_FLOOR")

        if fraud_score > 0.60:
            recommendation = "REFER"
            applied.append("FRAUD_SCORE_THRESHOLD")

        if risk_tier == "HIGH" and confidence < 0.70:
            recommendation = "REFER"
            applied.append("HIGH_RISK_CONFIDENCE_FLOOR")

        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "apply_hard_constraints",
            ["recommendation", "confidence", "fraud_score"],
            ["recommendation"],
            ms,
        )
        return {**state, "recommendation": recommendation, "hard_constraints_applied": applied}

    async def _node_write_output(self, state: OrchestratorState) -> OrchestratorState:
        t = time.time()
        app_id = state["application_id"]
        recommendation = state.get("recommendation", "REFER")
        approved_amount = state.get("approved_amount", 0.0)
        executive_summary = state.get("executive_summary", "")
        conditions = state.get("conditions") or []
        applied = state.get("hard_constraints_applied") or []

        # DecisionGenerated
        decision_event = BaseEvent(
            event_type="DecisionGenerated",
            payload={
                "application_id": app_id,
                "session_id": self.session_id,
                "recommendation": recommendation,
                "approved_amount_usd": approved_amount,
                "executive_summary": executive_summary,
                "conditions": conditions,
                "policy_overrides_applied": applied,
                "generated_at": datetime.now().isoformat(),
            },
        )
        await self._append_with_retry(f"loan-{app_id}", [decision_event])
        events_written = [{"stream_id": f"loan-{app_id}", "event_type": "DecisionGenerated"}]

        if recommendation == "APPROVE":
            outcome_event = BaseEvent(
                event_type="ApplicationApproved",
                payload={
                    "application_id": app_id,
                    "approved_amount_usd": approved_amount,
                    "conditions": conditions,
                    "approved_at": datetime.now().isoformat(),
                },
            )
            events_written.append(
                {"stream_id": f"loan-{app_id}", "event_type": "ApplicationApproved"}
            )
        elif recommendation == "DECLINE":
            outcome_event = BaseEvent(
                event_type="ApplicationDeclined",
                payload={
                    "application_id": app_id,
                    "reason": "Orchestrator decision: DECLINE",
                    "adverse_action_notice_required": True,
                    "declined_at": datetime.now().isoformat(),
                },
            )
            events_written.append(
                {"stream_id": f"loan-{app_id}", "event_type": "ApplicationDeclined"}
            )
        else:  # REFER
            outcome_event = BaseEvent(
                event_type="HumanReviewRequested",
                payload={
                    "application_id": app_id,
                    "reason": f"Recommendation: {recommendation}. Constraints applied: {applied}",
                    "requested_at": datetime.now().isoformat(),
                },
            )
            events_written.append(
                {"stream_id": f"loan-{app_id}", "event_type": "HumanReviewRequested"}
            )

        await self._append_with_retry(f"loan-{app_id}", [outcome_event])

        await self._record_output_written(
            events_written, f"Decision: {recommendation}, amount: ${approved_amount:,.0f}"
        )
        ms = int((time.time() - t) * 1000)
        await self._record_node_execution(
            "write_output", ["recommendation"], ["events_written"], ms
        )
        return {**state, "output_events": events_written, "next_agent": None}
