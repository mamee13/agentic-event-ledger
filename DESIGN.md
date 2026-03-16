# DESIGN.md — Architectural Decisions & Tradeoff Analysis

> This document is a living record of every significant architectural decision made during the build of The Ledger. Each section answers not just "what was built" but "why this and not that."

---

## 1. Aggregate Boundary Justification

> Why is `ComplianceRecord` a separate aggregate from `LoanApplication`? What would couple if you merged them? Trace the coupling to a specific failure mode under concurrent write scenarios.

*To be completed after Phase 2 implementation.*

---

## 2. Projection Strategy

> For each projection, justify: Inline vs. Async, and the SLO commitment. For `ComplianceAuditView` temporal query, justify your snapshot strategy and describe snapshot invalidation logic.

*To be completed after Phase 3 implementation.*

---

## 3. Concurrency Analysis

> Under peak load (100 concurrent applications, 4 agents each), how many `OptimisticConcurrencyErrors` do you expect per minute on `loan-{id}` streams? What is the retry strategy and what is the maximum retry budget before returning a failure to the caller?

*To be completed after Phase 2 implementation.*

---

## 4. Upcasting Inference Decisions

> For every inferred field in your upcasters, quantify the likely error rate and the downstream consequence of an incorrect inference. When would you choose null over an inference?

*To be completed after Phase 4 implementation.*

---

## 5. EventStoreDB Comparison

> Map your PostgreSQL schema to EventStoreDB concepts. What does EventStoreDB give you that your implementation must work harder to achieve?

*To be completed after Phase 4 implementation.*

---

## 6. What You Would Do Differently

> Name the single most significant architectural decision you would reconsider with another full day.

*To be completed at project end.*

---

## Schema Column Justification (Phase 1)

> Every column in the `events`, `event_streams`, `projection_checkpoints`, and `outbox` tables justified.

*To be completed after Phase 1 implementation.*

---

## Missing Events in the Event Catalogue

The challenge catalogue is intentionally incomplete. The following events are missing and should be added:

| Missing Event | Aggregate | Reason |
|---|---|---|
| `FraudScreeningRequested` | `LoanApplication` | Symmetry with `CreditAnalysisRequested` — the loan stream should record when fraud screening was initiated, not just when it completed |
| `AgentSessionClosed` | `AgentSession` | A session needs a terminal event to mark it as complete; without it, `reconstruct_agent_context` cannot distinguish an active session from an abandoned one |
| `ComplianceClearanceIssued` | `ComplianceRecord` | The aggregate needs a terminal event confirming all required checks passed; `ComplianceRulePassed` events alone do not constitute a clearance |
| `ApplicationWithdrawn` | `LoanApplication` | Applicants can withdraw; the state machine needs this transition to reach a terminal state without approval or decline |
| `HumanReviewOverride` | `LoanApplication` | The model version locking rule (business rule 3) references this event as the only way to supersede a completed credit analysis; it is referenced but not defined in the catalogue |
| `DecisionOrchestratorSessionStarted` | `AgentSession` | The `DecisionOrchestrator` is an agent and must follow the Gas Town pattern — it needs an `AgentContextLoaded` equivalent to start its session |
| `AuditStreamInitialised` | `AuditLedger` | The audit stream needs an identity event recording what entity it covers and when monitoring began |
