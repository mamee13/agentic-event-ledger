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

### ApplicationSummary (Async, SLO: 500ms lag)
This projection provides a flattened view of loan applications for real-time UI updates. It is updated asynchronously by the `ProjectionDaemon` to keep the write path fast.

### AgentPerformanceLedger (Async, SLO: 2s lag)
Aggregates metrics for agents. The 2s lag is acceptable as these metrics are typically used for reporting and monitoring, not hot-path decisions.

### ComplianceAuditView (Async, SLO: 2s lag)
Stores full event history per application to support temporal queries (`get_compliance_at`). Uses snapshots every 50 events to optimize rehydration for applications with long histories.

### Snapshot Strategy & Invalidation
- **Strategy**: Count-based snapshots. A snapshot of the rehydrated state is stored in `projection_snapshots` every 50 events for a given `application_id`.
- **Invalidation Rules**:
    - **Upcasting**: If a new version of an event is introduced and an upcaster is added that changes the payload structure, all existing snapshots for that projection must be invalidated (deleted) as the rehydrated state would be different.
    - **Logic Changes**: Significant changes to the `_rehydrate_compliance` logic require a full rebuild and snapshot invalidation.
    - **Zero-Downtime Rebuild**: The `rebuild_from_scratch()` method handles the migration to new schemas/logic by populating a side table before swapping, ensuring live reads continue to serve stale but consistent data until the swap.

---

## 3. Concurrency Analysis

> Under peak load (100 concurrent applications, 4 agents each), how many `OptimisticConcurrencyErrors` do you expect per minute on `loan-{id}` streams? What is the retry strategy and what is the maximum retry budget before returning a failure to the caller?

Under peak load (100 concurrent applications, 4 agents each), `OptimisticConcurrencyErrors` are expected when agents make overlapping decisions. Our implementation uses row-level locking (`SELECT ... FOR UPDATE`) on the `event_streams` table to serialize appends to the same stream. Agents that lose the race will receive an error and must reload the stream state before retrying. 

**Retry Strategy:** Agents should implement exponential backoff with a maximum of 3 retries. If the conflict persists, the agent should abort and signal a coordination failure to the Decision Orchestrator.

---

## 4. Upcasting Inference Decisions

> For every inferred field in your upcasters, quantify the likely error rate and the downstream consequence of an incorrect inference. When would you choose null over an inference?

### CreditAnalysisCompleted v1 → v2

| Field | Strategy | Error Rate | Consequence of Wrong Value | Decision |
|---|---|---|---|---|
| `model_version` | Inferred from `recorded_at` using a schedule of model deployment windows | ~1% (model swapped mid-second) | Wrong model attributed in audit trail; compliance report cites incorrect model | Infer — "unknown" fallback is safe and auditable |
| `confidence_score` | Always `null` | 0% | N/A | **Null** — hallucinating a score would corrupt downstream compliance decisions replayed from history |
| `regulatory_basis` | Inferred from regulatory schedule active at `recorded_at` | <0.1% (regulatory windows are well-defined) | Wrong regulation cited in audit package | Infer — windows are deterministic and verifiable |

### DecisionGenerated v1 → v2

| Field | Strategy | Error Rate | Consequence | Decision |
|---|---|---|---|---|
| `model_versions` | Reconstructed by loading contributing `AgentSession` streams | 0% if sessions exist; 100% if sessions were deleted | Missing model attribution in audit trail | Reconstruct — sessions are immutable append-only streams, deletion is a policy violation |

**Performance tradeoff for `model_versions` reconstruction:**
Each v1 `DecisionGenerated` upcaster call loads N `AgentSession` streams (one per entry in `contributing_agent_sessions`). On a local DB this adds ~1–5ms per event. For bulk replays (`rebuild_from_scratch`) this is acceptable. For hot-path reads the contributing sessions are typically already in memory. If this becomes a bottleneck, the fix is to persist `model_versions` at write time and only fall back to reconstruction for legacy v1 events. The `EventStore` supports injecting a pre-built `_session_model_cache` dict into the payload before upcasting to avoid redundant DB lookups.

### General rule: null vs inference

Choose **null** when:
- The field directly affects a compliance decision or audit assertion (wrong value is worse than missing value).
- The inference source is unavailable or unreliable at upcasting time.

Choose **inference** when:
- The field is metadata/attribution (model version, regulatory basis).
- The inference source is deterministic and verifiable (schedule-based, not ML-based).
- A wrong value is detectable and correctable without corrupting the audit chain.

---

## 5. EventStoreDB Comparison

> Map your PostgreSQL schema to EventStoreDB concepts. What does EventStoreDB give you that your implementation must work harder to achieve?

### Schema mapping

| This implementation | EventStoreDB concept | Notes |
|---|---|---|
| `events.stream_id` | Stream name | Direct equivalent. EventStoreDB uses string stream names; we use the same convention (`loan-{id}`, `agent-{id}-{id}`). |
| `events.stream_position` | Event number (per-stream) | EventStoreDB calls this the revision. Used identically for optimistic concurrency. |
| `events.global_position` (BIGSERIAL) | `$all` position | EventStoreDB maintains a global log (`$all`) with a monotonic position. We replicate this with a BIGSERIAL. |
| `events.event_type` | Event type | Direct equivalent. EventStoreDB uses this for server-side filtering and persistent subscriptions. |
| `events.payload` (JSONB) | Event data | EventStoreDB stores as bytes with a content-type header. We store as JSONB, which gives us indexed queries at the cost of schema flexibility. |
| `events.metadata` (JSONB) | Metadata | Direct equivalent. EventStoreDB separates data and metadata at the protocol level; we use a JSONB column. |
| `event_streams.current_version` | Stream revision | EventStoreDB tracks this internally. We maintain it explicitly in `event_streams` to support `SELECT ... FOR UPDATE` locking. |
| `outbox` table | Persistent subscriptions / catch-up subscriptions | EventStoreDB delivers events to subscribers natively. We implement at-least-once delivery manually via the outbox pattern. |
| `projection_checkpoints` | Subscription checkpoint | EventStoreDB checkpoints persistent subscriptions server-side. We store the last processed `global_position` per projection in this table. |

### What EventStoreDB gives you that we work harder to achieve

**Native competing consumers.** EventStoreDB's persistent subscriptions handle fan-out, load balancing, and at-least-once delivery natively. Our outbox table plus `ProjectionDaemon` replicates this but requires us to manage polling, checkpointing, and failure recovery manually.

**Server-side stream filtering.** EventStoreDB can filter the `$all` stream by event type or stream prefix at the server, returning only matching events. Our `load_all` method does this with a SQL `WHERE event_type = ANY(...)` clause, which is functionally equivalent but requires the filter logic to live in application code.

**Optimistic concurrency built into the write protocol.** EventStoreDB's append API accepts an `expectedRevision` parameter and enforces it atomically. We replicate this with `SELECT ... FOR UPDATE` on `event_streams`, which is correct but adds a round-trip and a row lock that EventStoreDB avoids by handling concurrency inside its storage engine.

**Projections as a first-class primitive.** EventStoreDB has a built-in JavaScript projection engine that can emit new events, maintain state, and partition by stream. Our `BaseProjection` + `ProjectionDaemon` covers the same ground but is entirely application-managed.

**Link events and system streams.** EventStoreDB can create `$by-event-type` and `$by-category` system streams automatically, enabling efficient reads like "all `DecisionGenerated` events across all loans." We approximate this with indexed queries on `event_type`, but there is no equivalent of a materialised stream pointer.

---

## 6. What You Would Do Differently

> Name the single most significant architectural decision you would reconsider with another full day.

**The compliance clearance write path.**

The current design writes `ComplianceRulePassed` / `ComplianceRuleFailed` to the `compliance-{id}` stream only, then emits a synthetic `ComplianceRulePassed` (with `rule_id = "compliance_clearance"`) to the loan stream when all rules pass. This was the right call for avoiding write contention between the `ComplianceAgent` and the loan lifecycle, but the implementation has a subtle problem: the clearance event is a fabricated event that does not correspond to any real domain fact. It exists purely to advance the loan state machine.

With another day I would introduce a dedicated `ComplianceClearanceIssued` event type — already identified in the missing events catalogue — and emit that to the loan stream instead. *(Update: This has now been implemented. `ComplianceClearanceIssued` is emitted explicitly to the loan stream.)* This makes the intent explicit in the event catalogue, removes the ambiguity of a `ComplianceRulePassed` event with a synthetic `rule_id`, and gives the `LoanApplicationAggregate` a clean, unambiguous trigger for the `COMPLIANCE_REVIEW → PENDING_DECISION` transition. The `ComplianceRecordAggregate` would emit this event as its terminal event, and the service layer would append it to the loan stream as a cross-stream write — the same pattern already used for `ComplianceCheckRequested`.

The broader lesson: when a state machine transition requires a signal from another aggregate, model that signal as a named domain event rather than reusing an existing event type with a special payload value. The latter works but leaks implementation detail into the event catalogue.

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

| Missing Event | Aggregate | Status / Reason |
|---|---|---|
| `FraudScreeningRequested` | `LoanApplication` | **[Implemented]** Symmetry with `CreditAnalysisRequested` — the loan stream should record when fraud screening was initiated, not just when it completed |
| `AgentSessionClosed` | `AgentSession` | A session needs a terminal event to mark it as complete; without it, `reconstruct_agent_context` cannot distinguish an active session from an abandoned one |
| `ComplianceClearanceIssued` | `ComplianceRecord` | **[Implemented]** The aggregate needs a terminal event confirming all required checks passed; `ComplianceRulePassed` events alone do not constitute a clearance |
| `ApplicationWithdrawn` | `LoanApplication` | **[Implemented]** Applicants can withdraw; the state machine needs this transition to reach a terminal state without approval or decline |
| `HumanReviewOverride` | `LoanApplication` | The model version locking rule (business rule 3) references this event as the only way to supersede a completed credit analysis; it is referenced but not defined in the catalogue |
| `DecisionOrchestratorSessionStarted` | `AgentSession` | The `DecisionOrchestrator` is an agent and must follow the Gas Town pattern — it needs an `AgentContextLoaded` equivalent to start its session |
| `AuditStreamInitialised` | `AuditLedger` | **[Implemented]** The audit stream needs an identity event recording what entity it covers and when monitoring began |

*(Note: `FraudScreeningRequested`, `ApplicationWithdrawn`, `ComplianceClearanceIssued`, and `AuditStreamInitialised` have now been implemented in the core models.)*
