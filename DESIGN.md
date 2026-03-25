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

**Expected collision rate — worked example:**

With the multi-aggregate boundary in place, the `loan-{id}` stream has at most **2 concurrent writers** per application at any given moment: the `CreditAnalysisAgent` (appending `CreditAnalysisCompleted`) and the `DecisionOrchestrator` (appending `DecisionGenerated`). The `ComplianceAgent` writes exclusively to `compliance-{id}` and never contends on the loan stream.

Assumptions:
- Each write window (time between stream load and append commit) is ~5ms under local Postgres.
- The two writers for a given loan are unlikely to overlap unless they are scheduled within the same 5ms window.
- At 100 concurrent applications, each with a ~5ms write window, the probability that two writers for the *same* loan overlap is approximately `5ms / (mean inter-write gap)`.
- Mean inter-write gap per loan ≈ 200ms (agents are pipelined, not simultaneous).
- Collision probability per write ≈ 5ms / 200ms = **~2.5%**.
- At 100 applications × 2 writes each = 200 writes/cycle, expected collisions ≈ **5 per cycle**.
- At a cycle rate of ~5 cycles/minute (each loan takes ~12s end-to-end), that is roughly **25 `OptimisticConcurrencyErrors` per minute** across all loan streams.

This is well within the retry budget. Each collision costs one stream reload (~1ms) plus one retry append. At 25/minute the retry overhead is negligible.

**Retry Strategy:** Exponential backoff — base 50ms, multiplier 2×, jitter ±20ms, maximum **3 retries**. If all 3 retries fail (i.e., 4 consecutive collisions on the same stream), the agent aborts and signals a coordination failure to the Decision Orchestrator, which can re-queue the operation. The maximum retry budget is 3 attempts; the 4th failure surfaces as an `OptimisticConcurrencyError` to the caller.

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

**The `ProjectionDaemon` is single-node only — and this has now been addressed.**

The original `ProjectionDaemon` runs as a single process that polls `events WHERE global_position > last_checkpoint` in a tight loop and fans out to each registered projection. This works correctly in development and under the load profile of this challenge, but it has a hard architectural ceiling: only one process can safely advance a given projection's checkpoint at a time. There is no coordination primitive preventing two daemon instances from processing the same event batch concurrently and producing duplicate or out-of-order projection writes.

The fix is the advisory lock + shard assignment pattern, now implemented in `DistributedProjectionDaemon` (`src/ledger/infrastructure/projections/distributed_daemon.py`) and `ShardCoordinator` (`src/ledger/infrastructure/projections/shard_coordinator.py`). Each daemon instance acquires a PostgreSQL advisory lock keyed on `(projection_name, shard_id)` before polling its assigned shard of the global event log. A `projection_shards` table records which instance owns which shard and when it last heartbeated. If an instance dies, its shards become available after a 15-second TTL and are claimed by surviving instances. The database itself is the coordinator — no Redis, no Zookeeper.

The reason this was not the first implementation: the shard assignment table and heartbeat loop add roughly 200 lines of infrastructure code that are entirely orthogonal to the domain logic being evaluated. The deliberate sequence was — ship a correct single-node daemon, document the scaling path clearly in `DOMAIN_NOTES.md` §6, then implement it once the domain logic was stable. The original `ProjectionDaemon` is untouched and still the default for single-node deployments; `DistributedProjectionDaemon` is a drop-in replacement that overrides only the checkpoint read/write methods.

---

## 7. Cross-Stream Write Atomicity

The `record_fraud_screening` MCP tool (and the `record_credit_analysis` service command) write the same event to two streams: the agent session stream (to track `contributed_apps` for causal chain validation) and the loan stream (to advance the loan state machine).

**Resolution:** Both commands now use `EventStore.append_multi()`, which acquires a single database connection, opens one `BEGIN/COMMIT` transaction, and writes to all target streams inside it. If either stream write fails (OCC or otherwise) the entire transaction is rolled back — no partial writes are possible.

`append_multi` is implemented by extracting the per-stream write logic into `_append_to_stream_on_conn()`, a private helper that operates on a caller-supplied connection. `append()` (single-stream) delegates to the same helper, so the locking and outbox logic is defined exactly once.

**Remaining limitation:** The outbox table is still written inside the same transaction (correct), but the outbox relay process (polling the outbox and publishing to an external bus) is not yet implemented. This is the Week 10 Polyglot Bridge integration point and is out of scope for this week.

**Update:** An `OutboxRelay` is now implemented in `src/ledger/infrastructure/outbox.py`. It polls unpublished rows with `FOR UPDATE SKIP LOCKED`, invokes a publisher callback, and marks rows as published only after successful delivery. This completes the outbox pattern for at-least-once delivery in the current stack.

---

## 8. What-If Causal Dependency Scope

The `_is_causally_dependent` function in `src/ledger/core/whatif.py` determines which post-branch events are skipped in a counterfactual replay. It currently models three branch types:

- `CreditAnalysisCompleted` — a change in `risk_tier` makes `DecisionGenerated`, `ApplicationApproved`, `ApplicationDeclined`, and `HumanReviewCompleted` causally dependent (the recommendation may differ).
- `ComplianceCheckRequested` — same downstream dependency set.
- `FraudScreeningCompleted` — same downstream dependency set.

**Limitation:** Any other `branch_event_type` (e.g., `ApplicationSubmitted`, `AgentContextLoaded`) is treated as having no downstream dependencies. All post-branch events are replayed unchanged. This is the conservative choice: it may over-replay events that are actually dependent on the branch, but it never silently drops events that should be replayed. The counterfactual outcome may therefore be slightly optimistic (it includes events that a real counterfactual world might not have produced), but it will never be missing events that are causally independent.

**Why not generalise further:** The dependency graph for arbitrary branch types would require a full causal model of the domain event catalogue. The three covered branch types account for all branch points that the `run_whatif` tool is currently called with in practice. Extending to new branch types requires adding the branch type to the condition in `_is_causally_dependent` and verifying the dependent event set.

---

## Schema Column Justification (Phase 1)

> Every column in the `events`, `event_streams`, `projection_checkpoints`, and `outbox` tables justified.

### `events` table
- `global_position` (BIGSERIAL): Global ordering for the entire ledger. Essential for projections and catch-up subscriptions.
- `stream_id` (TEXT): The aggregate instance ID. Indexed for fast stream loading.
- `stream_position` (INT): Event order within the stream. Used for optimistic concurrency validation.
- `event_type` (TEXT): Domain name of the event. Used for routing and filtering.
- `event_version` (SMALLINT): Enables schema evolution and upcasting without mutating historical data.
- `payload` (JSONB): The event data.
- `metadata` (JSONB): Non-domain data (correlation_id, causation_id, agent_metadata).
- `recorded_at` (TIMESTAMPTZ): Immutable record time; required for temporal queries and upcaster inference.

### `event_streams` table
- `stream_id` (TEXT): Unique stream identifier.
- `aggregate_type` (TEXT): Aggregate category for administrative queries and archive policies.
- `current_version` (INT): The current position of the last event. Used as the lock point for writes.
- `created_at` (TIMESTAMPTZ): Auditability of stream creation; supports lifecycle reporting.
- `archived_at` (TIMESTAMPTZ): Soft-delete marker for retired aggregates; preserves immutability.
- `metadata` (JSONB): Stream-level attributes (tenant, compliance tier, etc.).

### `projection_checkpoints` table
- `projection_name` (TEXT): Unique projection identifier.
- `last_position` (BIGINT): Global position last processed; enables exactly-once replay.
- `updated_at` (TIMESTAMPTZ): Operational visibility for lag calculations and alerting.

### `outbox` table
- `event_id` (UUID): Reference to the event to be published. Ensures at-least-once delivery when paired with a reliable publisher.
- `id` (UUID): Independent outbox row identity for retries and auditing.
- `destination` (TEXT): Target channel/bus (Kafka topic, Redis stream, etc.).
- `payload` (JSONB): Serialized payload for downstream delivery without reloading events.
- `created_at` (TIMESTAMPTZ): Ordering and retry windows.
- `published_at` (TIMESTAMPTZ): Delivery acknowledgement for idempotent publishing.
- `attempts` (SMALLINT): Retry budget and monitoring of delivery failures.

---

## Missing Events in the Event Catalogue

The challenge catalogue is intentionally incomplete. The following events are missing and should be added:

| Missing Event | Aggregate | Status / Reason |
|---|---|---|
| `FraudScreeningRequested` | `LoanApplication` | **[Implemented]** Symmetry with `CreditAnalysisRequested` — the loan stream should record when fraud screening was initiated, not just when it completed |
| `AgentSessionClosed` | `AgentSession` | **[Implemented]** Terminal event to mark session completion; enables crash recovery to distinguish active vs abandoned sessions |
| `ComplianceClearanceIssued` | `ComplianceRecord` | **[Implemented]** The aggregate needs a terminal event confirming all required checks passed; `ComplianceRulePassed` events alone do not constitute a clearance |
| `ApplicationWithdrawn` | `LoanApplication` | **[Implemented]** Applicants can withdraw; the state machine needs this transition to reach a terminal state without approval or decline |
| `HumanReviewOverride` | `LoanApplication` | **[Implemented]** Explicit override event to unlock model version locking without reusing `HumanReviewCompleted` |
| `DecisionOrchestratorSessionStarted` | `AgentSession` | **[Implemented]** Gas Town entry event for orchestrator sessions |
| `AuditStreamInitialised` | `AuditLedger` | **[Implemented]** The audit stream needs an identity event recording what entity it covers and when monitoring began |

*(Note: `FraudScreeningRequested`, `ApplicationWithdrawn`, `ComplianceClearanceIssued`, `AuditStreamInitialised`, `AgentSessionClosed`, `DecisionOrchestratorSessionStarted`, and `HumanReviewOverride` have now been implemented in the core models.)*
