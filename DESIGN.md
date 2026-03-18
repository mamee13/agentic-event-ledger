# DESIGN.md — Architectural Decisions & Tradeoff Analysis

> This document is a living record of every significant architectural decision made during the build of The Ledger. Each section answers not just "what was built" but "why this and not that."

---

## Aggregate Boundary Reasoning

The transition to a multi-aggregate architecture was driven by the need for high-concurrency and strict auditability. Four aggregates were chosen: `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`.

### Rejected boundary: merging ComplianceRecord into LoanApplication

The obvious alternative is a single `loan-{id}` stream that contains both loan lifecycle events (`CreditAnalysisRequested`, `DecisionGenerated`, etc.) and compliance rule events (`ComplianceRulePassed`, `ComplianceRuleFailed`). This was rejected.

**The specific coupling failure under concurrent writes:**

The compliance checks (KYC, AML, fraud) are executed by a dedicated `ComplianceAgent` that runs concurrently with the `CreditAnalysisAgent` and `FraudDetectionAgent`. Under the merged boundary, every compliance rule result must be appended to `loan-{id}`. Each append requires acquiring the row-level lock on `event_streams WHERE stream_id = 'loan-{id}'` via `SELECT ... FOR UPDATE`.

With three concurrent agents all targeting the same stream, the collision sequence is:

1. `CreditAnalysisAgent` reads `current_version = 4`, holds the lock, inserts at `stream_position = 5`, commits.
2. `ComplianceAgent` was also waiting at `expected_version = 4`. It acquires the lock, reads `current_version = 5`, sees a mismatch, raises `OptimisticConcurrencyError`, rolls back.
3. `ComplianceAgent` reloads the stream (now 5 events), retries with `expected_version = 5`.
4. Meanwhile `FraudDetectionAgent` also read at version 4 and is retrying at version 5.
5. One wins; the other retries again.

At 1,000 applications/hour with 3–4 concurrent agents per application, the retry rate on `loan-{id}` streams becomes high enough to cause retry storms. Each retry requires a full stream reload plus re-evaluation of business logic. Under sustained load this degrades write throughput and increases tail latency on the loan lifecycle — the exact stream that the human reviewer and orchestrator are also writing to.

**The fix:** `ComplianceRulePassed` and `ComplianceRuleFailed` go to `compliance-{id}` only. The `ComplianceAgent` never contends with the loan lifecycle. The `LoanApplicationAggregate` reads compliance state lazily at approval time via a single `SELECT` on the compliance stream — no lock held on the loan stream during that read.

### LoanApplication vs. AgentSession

Each agent session is its own aggregate on `agent-{agent_id}-{session_id}`. This enforces the Gas Town pattern (context must be loaded before any analysis event) and means agents record their internal work — context loading, analysis steps, confidence scoring — without ever locking the loan stream. The loan stream lock is only acquired at the moment the agent appends a `CreditAnalysisCompleted` or `DecisionGenerated` event, which is a single atomic write, not a multi-step process.

**Coupling tradeoff:** The causal chain rule (Rule 6) requires that `DecisionGenerated.contributing_agent_sessions[]` references sessions that actually processed this loan. This means the `generate_decision` command handler must load each contributing `AgentSession` stream to verify `contributed_apps` before appending. This is N extra reads (one per contributing session) on the hot path. The tradeoff is accepted: reads are cheap and parallelisable; the alternative (embedding session state in the loan stream) would reintroduce write contention.

### AuditLedger

The `AuditLedger` aggregate lives on `audit-{entity_type}-{entity_id}`. It is append-only and written only by the integrity check process, never by the loan lifecycle or agent session flows. Keeping it separate means the cryptographic hash chain (Phase 5) can be computed and verified without touching any other stream.

---

## 2. Projection Strategy

> For each projection, justify: Inline vs. Async, and the SLO commitment. For `ComplianceAuditView` temporal query, justify your snapshot strategy and describe snapshot invalidation logic.

*To be completed after Phase 3 implementation.*

---

## 3. Concurrency Analysis

> Under peak load (100 concurrent applications, 4 agents each), how many `OptimisticConcurrencyErrors` do you expect per minute on `loan-{id}` streams? What is the retry strategy and what is the maximum retry budget before returning a failure to the caller?

Under peak load (100 concurrent applications, 4 agents each), `OptimisticConcurrencyErrors` are expected when agents make overlapping decisions. Our implementation uses row-level locking (`SELECT ... FOR UPDATE`) on the `event_streams` table to serialize appends to the same stream. Agents that lose the race will receive an error and must reload the stream state before retrying. 

**Retry Strategy:** Agents should implement exponential backoff with a maximum of 3 retries. If the conflict persists, the agent should abort and signal a coordination failure to the Decision Orchestrator.

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

### `events` table
- `global_position` (BIGSERIAL): Global ordering for the entire ledger. Essential for projections and catch-up subscriptions.
- `stream_id` (TEXT): The aggregate instance ID. Indexed for fast stream loading.
- `stream_position` (INT): Event order within the stream. Used for optimistic concurrency validation.
- `event_type` (TEXT): Domain name of the event. Used for routing and filtering.
- `payload` (JSONB): The event data.
- `metadata` (JSONB): Non-domain data (correlation_id, causation_id, agent_metadata).

### `event_streams` table
- `stream_id` (TEXT): Unique stream identifier.
- `current_version` (INT): The current position of the last event. Used as the lock point for writes.

### `outbox` table
- `event_id` (UUID): Reference to the event to be published. Ensures at-least-once delivery when paired with a reliable publisher.

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
