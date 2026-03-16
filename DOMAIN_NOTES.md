# DOMAIN_NOTES.md — Phase 0 Domain Reconnaissance

## 1. EDA vs. ES Distinction

A component that uses LangChain callbacks to capture trace data is **Event-Driven Architecture (EDA)**, not Event Sourcing. The callbacks fire notifications — "this step completed, here is the output" — and the data flows to a trace collector. If the collector is down, the trace is lost. The callback system has no concept of a stream, no stream position, no optimistic concurrency, and no guarantee that replaying the callbacks would reconstruct the agent's state.

**What changes if redesigned with The Ledger:**

| Before (EDA / LangChain callbacks) | After (Event Sourcing / The Ledger) |
|---|---|
| Callbacks write to a trace sink; sink can drop events | Every agent action appended to `agent-{id}-{session}` stream; ACID-guaranteed |
| Trace is a log of what happened, not the source of truth | The event stream IS the source of truth; current state is derived from it |
| Agent context is in-memory; lost on crash | Agent replays its stream on restart via `reconstruct_agent_context()` |
| No version tracking; no concurrency control | `expected_version` on every append; two agents cannot corrupt the same stream |
| Cannot answer "what was the agent's state at 14:32:07?" | `load_stream(to_position=N)` or temporal query gives exact state at any point |

**What you gain:** tamper-evident audit trail, crash recovery without repeated work, temporal queries for compliance, and the ability to add new read models (projections) retroactively from the full history.

---

## 2. The Aggregate Question

**The four aggregates chosen:** `LoanApplication`, `AgentSession`, `ComplianceRecord`, `AuditLedger`.

**Rejected boundary:** Merge `ComplianceRecord` into `LoanApplication`.
**Reason:** Introduces write contention between concurrent agents, causing retry storms on the `loan-{id}` stream.

**Alternative boundary considered and rejected:** Merging `ComplianceRecord` into `LoanApplication` as a nested sub-entity — one stream `loan-{id}` containing both loan lifecycle events and compliance rule events.

**Why it was rejected:**

The compliance checks are executed by a separate `ComplianceAgent` that runs concurrently with the `CreditAnalysis` and `FraudDetection` agents. If compliance events live in the `loan-{id}` stream, every compliance rule evaluation (`ComplianceRulePassed`, `ComplianceRuleFailed`) must append to the same stream as the credit analysis and fraud screening events. Under concurrent load, all three agents are contending for the same `stream_position` lock on `loan-{id}`.

**The specific failure mode this prevents:** At 1,000 applications/hour with 4 agents each, the `loan-{id}` stream becomes a write bottleneck. With merged aggregates, a `ComplianceRulePassed` append at `expected_version=5` collides with a `CreditAnalysisCompleted` append also at `expected_version=5`. One wins; the other gets `OptimisticConcurrencyError` and retries. With 3 concurrent agents on one stream, the collision rate is high enough to cause retry storms. Separating `ComplianceRecord` into its own stream means compliance appends never contend with loan lifecycle appends — each stream has one logical writer at a time.

---

## 3. Concurrency in Practice

**Scenario:** Two agents both read `loan-abc123` at `stream_position=3` and call `append(expected_version=3)` simultaneously.

**Exact sequence:**

1. Agent A and Agent B both execute `SELECT current_version FROM event_streams WHERE stream_id = 'loan-abc123' FOR UPDATE` — this is a row-level lock on the `event_streams` row.
2. PostgreSQL serialises the two `FOR UPDATE` requests. Agent A acquires the lock first.
3. Agent A reads `current_version = 3`, confirms it matches `expected_version = 3`, inserts its event at `stream_position = 4`, updates `event_streams.current_version = 4`, writes to outbox, and commits. Lock released.
4. Agent B acquires the lock. Reads `current_version = 4`. Its `expected_version = 3` does not match. The application layer raises `OptimisticConcurrencyError`. Transaction rolled back.
5. **What the losing agent receives:** `OptimisticConcurrencyError` with `stream_id`, `expected_version=3`, `actual_version=4`.
6. **What it must do next:** Reload the stream (`load_stream("loan-abc123")`), reconstruct the aggregate state from the 4 events now present, re-evaluate whether its analysis is still valid given the new event at position 4, and if so, retry the append with `expected_version=4`.

The losing agent must not blindly retry — it must re-run its business logic against the updated state. The new event at position 4 may have already superseded its intended action (e.g., another agent already completed the credit analysis).

---

## 4. Projection Lag and Its Consequences

**Scenario:** Loan officer queries "available credit limit" 50ms after an agent commits a `DisbursementRecorded` event. The `ApplicationSummary` projection has a typical lag of 200ms and has not yet processed the event.

**What the system does:**

The query hits the `ApplicationSummary` projection table, which still shows the pre-disbursement credit limit. The response is stale but not incorrect from the projection's perspective — it reflects the last processed state.

**How to communicate this to the UI:**

1. Every projection response includes a `as_of_position` field — the `global_position` of the last event the projection has processed.
2. The UI compares `as_of_position` against the `global_position` returned when the disbursement was committed (stored client-side after the write).
3. If `as_of_position < write_position`, the UI renders a subtle indicator: *"Balance updating — last refreshed N ms ago"* and polls the health endpoint (`ledger://ledger/health`) until the projection catches up.
4. For the specific "available credit limit" field, the UI can also implement **read-after-write consistency** at the application layer: after a disbursement command succeeds, the client holds the new limit locally and displays it immediately, treating the projection as eventually consistent background confirmation.

**SLO commitments:** `ApplicationSummary` < 500ms lag, `ComplianceAuditView` < 2s lag. Both enforced via `get_lag()` metrics exposed on the health endpoint and asserted in the SLO test suite. If `ApplicationSummary` lag exceeds 500ms, the health endpoint returns `WARNING` and the UI can display a degraded-mode banner.

---

## 5. The Upcasting Scenario

**Original schema (v1, defined 2024):**
```python
# CreditDecisionMade v1
{
    "application_id": "uuid",
    "decision": "APPROVE | DECLINE | REFER",
    "reason": "string"
}
```

**Required schema (v2, 2026):**
```python
# CreditDecisionMade v2
{
    "application_id": "uuid",
    "decision": "APPROVE | DECLINE | REFER",
    "reason": "string",
    "model_version": "string",
    "confidence_score": float | None,
    "regulatory_basis": list[str]
}
```

**The upcaster:**
```python
@registry.register("CreditDecisionMade", from_version=1)
def upcast_credit_decision_v1_to_v2(payload: dict) -> dict:
    return {
        **payload,
        "model_version": _infer_model_version(payload.get("recorded_at")),
        "confidence_score": None,
        "regulatory_basis": _infer_regulatory_basis(payload.get("recorded_at")),
    }

def _infer_model_version(recorded_at: str | None) -> str:
    # Model deployment history is a known, auditable record.
    # Map recorded_at timestamp ranges to the model version that was
    # in production at that time. This is a lookup against a static
    # deployment log, not a fabrication.
    if recorded_at is None:
        return "unknown-pre-2026"
    dt = datetime.fromisoformat(recorded_at)
    if dt < datetime(2025, 6, 1):
        return "credit-model-v1.x-legacy"
    if dt < datetime(2026, 1, 1):
        return "credit-model-v2.x-legacy"
    return "unknown-pre-2026"

def _infer_regulatory_basis(recorded_at: str | None) -> list[str]:
    # Regulation sets are versioned and their effective dates are known.
    # Infer which regulation set was active at the time of the decision.
    if recorded_at is None:
        return []
    dt = datetime.fromisoformat(recorded_at)
    if dt < datetime(2025, 1, 1):
        return ["REG-2024-BASEL-III", "REG-2024-AML-v2"]
    return ["REG-2025-BASEL-III", "REG-2025-AML-v3"]
```

**Note on implementation upcasters:** The example above uses `CreditDecisionMade` to illustrate the pattern. In Phase 4 implementation, the actual upcasters are `CreditAnalysisCompleted` v1→v2 (adds `model_version`, `confidence_score`, `regulatory_basis`) and `DecisionGenerated` v1→v2 (adds `model_versions{}` dict reconstructed from contributing agent sessions). The inference strategy and null-vs-fabrication reasoning above applies directly to both.

**Why `confidence_score` is `None` and not inferred:** The confidence score is a numerical output of the model at inference time. It was never stored in v1. There is no deterministic way to reconstruct it — re-running the model on the original inputs would require the original inputs (not stored) and the original model weights (may be unavailable). Setting it to `None` is honest. Fabricating a value (e.g., `0.75` as a "typical" score) would cause downstream systems — compliance checks, regulatory reports — to treat a fabricated number as a real model output. The downstream consequence of a fabricated confidence score is a compliance violation. `None` forces downstream consumers to handle the unknown case explicitly.

---

## 6. The Marten Async Daemon Parallel

**What Marten 7.0 introduced:** Distributed projection execution where multiple application nodes each run a shard of the projection workload. A coordination layer (backed by the database) assigns stream ranges to nodes, detects node failures, and redistributes shards. No single node is a bottleneck; projection throughput scales horizontally.

**Python equivalent:**

The coordination primitive is a **PostgreSQL advisory lock** combined with a **shard assignment table**.

```
projection_shards (
    shard_id        TEXT,
    projection_name TEXT,
    assigned_node   TEXT,       -- hostname or pod ID
    heartbeat_at    TIMESTAMPTZ,
    global_pos_from BIGINT,
    global_pos_to   BIGINT      -- NULL = open-ended (tail shard)
)
```

Each Python daemon node on startup:
1. Attempts to acquire a PostgreSQL advisory lock for a shard (`pg_try_advisory_lock(shard_id_hash)`).
2. If acquired, registers itself in `projection_shards` and begins processing events in its assigned `global_position` range.
3. Updates `heartbeat_at` every 5 seconds.

A **coordinator process** (or the nodes themselves via leader election using `pg_try_advisory_lock`) monitors `heartbeat_at`. If a node's heartbeat is stale by >15 seconds, its shard is released and another node claims it.

**Failure mode this guards against:** A single projection daemon node crashing mid-batch. Without distributed coordination, the projection stops until the node restarts. With shard redistribution, another node detects the stale heartbeat within 15 seconds and resumes from the dead node's last checkpoint. The checkpoint-per-shard in `projection_checkpoints` ensures no events are skipped or double-processed.

The key difference from Marten: Marten's daemon is built into the framework with battle-tested shard rebalancing. The Python equivalent requires explicit implementation of the heartbeat, leader election, and shard assignment logic — more code, same pattern.

**Inference strategy for `model_version`:** The model deployment history is a known, auditable external record (deployment logs, MLflow registry). Mapping `recorded_at` to a model version is a deterministic lookup, not a guess. The error rate is low for events with accurate timestamps; it is zero for events after version tracking was introduced.
