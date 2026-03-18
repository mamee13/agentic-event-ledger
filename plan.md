# TRP1 Week 5 Ledger — Phase Plan (with Bonus)

**Summary**
This plan breaks the work into clear phases, each with checkboxes you can tick off. Phase 0 stays on `main`, then each phase gets its own technical branch (no numbers in names). Tooling is standardized with uv only, plus ruff, mypy, pytest, and pre-commit. Docker is added at the end. The bonus phase is included as its own section.

**Branch List (no numbers)**
- `domain-recon`
- `event-store-core`
- `domain-aggregates`
- `projections-daemon`
- `upcasting-integrity`
- `mcp-interface`
- `what-if-regulatory` (bonus)

---

## Phase 0 — Domain Recon (on `main`)
- [ ] Write `DOMAIN_NOTES.md` answer 1 with a clear, concrete EDA vs ES contrast based on this project.
- [ ] Explain exactly what changes in architecture when using The Ledger and what governance/audit benefits appear.
- [ ] Write `DOMAIN_NOTES.md` answer 2 listing the four aggregates and one rejected alternative boundary.
- [ ] State the specific coupling failure the rejected boundary would cause under concurrent writes.
- [ ] Write `DOMAIN_NOTES.md` answer 3 with a step-by-step optimistic concurrency sequence and the losing agent’s retry behavior.
- [ ] Write `DOMAIN_NOTES.md` answer 4 describing projection lag, the stale-read user experience, and how the UI communicates it.
- [ ] Write `DOMAIN_NOTES.md` answer 5 with an explicit upcaster and your inference strategy for missing fields.
- [ ] Write `DOMAIN_NOTES.md` answer 6 describing Python distributed projection execution, coordination primitive, and failure mode avoided.
- [ ] Create `DESIGN.md` with the six required section headings and a short placeholder for each.
- [ ] Identify missing events in the Event Catalogue and list them in `DOMAIN_NOTES.md` with why each is needed.

---

## Phase 1 — Tooling Baseline (branch: `domain-recon`)
- [ ] Define **uv-only** workflow commands and document them (no pip/poetry):
- [ ] `uv sync` (or equivalent) for dependency install
- [ ] `uv run ruff check .` for lint
- [ ] `uv run ruff format .` for formatting
- [ ] `uv run mypy .` for type checks
- [ ] `uv run pytest` for tests
- [ ] Configure ruff to enforce both linting and formatting rules:
- [ ] Enable format rules (ruff-format or equivalent) and align line-length with project standard.
- [ ] Enable lint rules that catch unused imports, unused variables, and unsafe practices in async code.
- [ ] Configure mypy to catch API contract mistakes:
- [ ] Enable strict optional checking and disallow implicit Any for public interfaces.
- [ ] Ensure mypy checks async functions and return types on EventStore and aggregates.
- [ ] Set up pytest for async tests:
- [ ] Add pytest-asyncio and configure asyncio mode.
- [ ] Create a baseline test layout so unit vs integration tests are clearly separated.
- [ ] Add pre-commit hooks that always run before commits:
- [ ] ruff lint, ruff format, mypy
- [ ] Ensure hooks use uv for consistency and do not call pip.
- [ ] Update README with a short dev workflow:
- [ ] How to install deps with uv
- [ ] How to run lint/format/typecheck/test
- [ ] How to run pre-commit hooks

---

## Phase 2 — Event Store Core (branch: `event-store-core`)
- [ ] Create the Postgres schema with these **exact tables and columns**:
- [ ] `events` table with: `event_id (UUID PK, default gen_random_uuid)`, `stream_id (TEXT NOT NULL)`, `stream_position (BIGINT NOT NULL)`, `global_position (BIGINT GENERATED ALWAYS AS IDENTITY)`, `event_type (TEXT NOT NULL)`, `event_version (SMALLINT NOT NULL DEFAULT 1)`, `payload (JSONB NOT NULL)`, `metadata (JSONB NOT NULL DEFAULT '{}'::jsonb)`, `recorded_at (TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp)`, plus `UNIQUE (stream_id, stream_position)`.
- [ ] `event_streams` table with: `stream_id (TEXT PK)`, `aggregate_type (TEXT NOT NULL)`, `current_version (BIGINT NOT NULL DEFAULT 0)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())`, `archived_at (TIMESTAMPTZ NULL)`, `metadata (JSONB NOT NULL DEFAULT '{}'::jsonb)`.
- [ ] `projection_checkpoints` table with: `projection_name (TEXT PK)`, `last_position (BIGINT NOT NULL DEFAULT 0)`, `updated_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())`.
- [ ] `outbox` table with: `id (UUID PK DEFAULT gen_random_uuid)`, `event_id (UUID NOT NULL REFERENCES events(event_id))`, `destination (TEXT NOT NULL)`, `payload (JSONB NOT NULL)`, `created_at (TIMESTAMPTZ NOT NULL DEFAULT NOW())`, `published_at (TIMESTAMPTZ NULL)`, `attempts (SMALLINT NOT NULL DEFAULT 0)`.
- [ ] Add indexes exactly as specified:
- [ ] `idx_events_stream_id` on `events(stream_id, stream_position)`
- [ ] `idx_events_global_pos` on `events(global_position)`
- [ ] `idx_events_type` on `events(event_type)`
- [ ] `idx_events_recorded` on `events(recorded_at)`
- [ ] Implement EventStore.append with these behaviors:
- [ ] Load current stream version and compare to expected_version (-1 for new stream; exact match required otherwise).
- [ ] Insert new events with strictly increasing stream_position and correct global_position ordering.
- [ ] Write outbox rows in the same DB transaction as events.
- [ ] Return the new stream version or raise OptimisticConcurrencyError with expected/actual versions.
- [ ] Implement EventStore.load_stream to:
- [ ] Read events by stream_id ordered by stream_position.
- [ ] Apply automatic upcasting before returning events.
- [ ] Support from_position and to_position filtering.
- [ ] Implement EventStore.load_all to:
- [ ] Stream events from the global position in batches.
- [ ] Support optional event_types filtering.
- [ ] Expose as an async iterator suitable for replays.
- [ ] Implement stream_version, archive_stream, and get_stream_metadata with correct semantics and error handling.
- [ ] Add the double-decision concurrency test:
- [ ] Two concurrent appends at expected_version=3.
- [ ] Exactly one succeeds and the other raises OptimisticConcurrencyError.
- [ ] Assert stream_position=4 for the winner and total stream length of 4.
- [ ] Add unit tests for each EventStore method covering both success and failure paths.
- [ ] Update `DESIGN.md` with justification for every schema column and index choice.

---

## Phase 3 — Domain Logic (branch: `domain-aggregates`)
- [ ] Implement LoanApplicationAggregate with explicit apply handlers for these events: ApplicationSubmitted, CreditAnalysisRequested, DecisionGenerated (v2), HumanReviewCompleted, ApplicationApproved, ApplicationDeclined.
- [ ] Implement AgentSessionAggregate with explicit apply handlers for these events: AgentContextLoaded, CreditAnalysisCompleted (v2), FraudScreeningCompleted (v1).
- [ ] Implement ComplianceRecord aggregate with explicit apply handlers for these events: ComplianceCheckRequested, ComplianceRulePassed, ComplianceRuleFailed.
- [ ] Ensure each aggregate implements `load()` that replays its own stream in order and sets `version` to the last event position.
- [ ] Enforce the exact LoanApplication state machine: Submitted → AwaitingAnalysis → AnalysisComplete → ComplianceReview → PendingDecision → ApprovedPendingHuman/DeclinedPendingHuman → FinalApproved/FinalDeclined.
- [ ] Enforce AgentSession Gas Town rule: AgentContextLoaded must be the first event; no decision event before it.
- [ ] Enforce model-version locking in the LoanApplication aggregate: after first CreditAnalysisCompleted for an application, reject any further CreditAnalysisCompleted unless a HumanReviewCompleted with override=True has occurred.
- [ ] Enforce confidence floor in the LoanApplication aggregate: DecisionGenerated with confidence_score < 0.6 must set recommendation="REFER".
- [ ] Enforce compliance dependency in the LoanApplication aggregate by reading ComplianceRecord stream: ApplicationApproved cannot be appended unless all required ComplianceRulePassed events exist for that application.
- [ ] Enforce causal chain in the LoanApplication aggregate: DecisionGenerated.contributing_agent_sessions[] must reference AgentSession stream IDs that contain a decision event for this application_id.
- [ ] Implement command handlers that follow the exact pattern: load aggregates → validate rules → build events → append with expected_version.
- [ ] Use the exact stream ID formats from the brief: loan-{application_id}, agent-{agent_id}-{session_id}, compliance-{application_id}, audit-{entity_type}-{entity_id}.
- [ ] Use the exact payload field names from the event catalogue (e.g., DecisionGenerated uses recommendation, not decision).
- [ ] Add unit tests for each rule with negative cases for missing prerequisites.
- [ ] Add tests for invalid transitions (e.g., DecisionGenerated from AnalysisComplete).
- [ ] Add tests that reject DecisionGenerated with contributing_agent_sessions that did not process this application_id.
- [ ] Update `DESIGN.md` with aggregate boundary reasoning and coupling tradeoffs (explicitly explain why ComplianceRecord is separate from LoanApplication).

---

## Phase 4 — Projections + Daemon (branch: `projections-daemon`)
- [ ] Implement ProjectionDaemon with explicit methods:
- [ ] run_forever(poll_interval_ms=100) that loops, calls _process_batch, sleeps.
- [ ] _process_batch loads from the lowest projection checkpoint, processes events in batches, and updates checkpoints.
- [ ] Route each event only to projections that subscribe to that event type.
- [ ] Fault tolerance: log projection errors, retry a limited number of times, then skip the offending event and continue.
- [ ] Lag measurement per projection: global_position - last_processed_position (exposed via get_lag()).
- [ ] Build ApplicationSummary projection with the exact table schema from the brief:
- [ ] application_id, state, applicant_id, requested_amount_usd, approved_amount_usd,
- [ ] risk_tier, fraud_score, compliance_status, decision,
- [ ] agent_sessions_completed[], last_event_type, last_event_at,
- [ ] human_reviewer_id, final_decision_at.
- [ ] Update ApplicationSummary on every relevant event and always set last_event_type/last_event_at.
- [ ] Build AgentPerformanceLedger projection with the exact table schema from the brief:
- [ ] agent_id, model_version, analyses_completed, decisions_generated,
- [ ] avg_confidence_score, avg_duration_ms,
- [ ] approve_rate, decline_rate, refer_rate,
- [ ] human_override_rate, first_seen_at, last_seen_at.
- [ ] Build ComplianceAuditView projection with full compliance history and temporal query support:
- [ ] get_current_compliance(application_id) returns full compliance record.
- [ ] get_compliance_at(application_id, timestamp) returns state as of timestamp.
- [ ] get_projection_lag() returns ms between latest store event and last processed.
- [ ] rebuild_from_scratch() replays all events without downtime to live reads.
- [ ] Define a snapshot strategy (time-based, count-based, or manual) and document snapshot invalidation in `DESIGN.md`.
- [ ] Add tests that simulate 50 concurrent command handlers and assert lag SLOs:
- [ ] ApplicationSummary p99 lag < 500ms.
- [ ] ComplianceAuditView p99 lag < 2s.

---

## Phase 5 — Upcasting + Integrity + Gas Town (branch: `upcasting-integrity`)
- [ ] Implement UpcasterRegistry with decorator-based registration and chained upcasting by version.
- [ ] Ensure EventStore.load_stream/load_all automatically apply upcasters transparently (callers never invoke them).
- [ ] Implement CreditAnalysisCompleted v1 → v2 upcaster:
- [ ] Add model_version inferred from recorded_at timestamp rule (documented).
- [ ] Add confidence_score = null when truly unknown.
- [ ] Add regulatory_basis inferred from rule set active at recorded_at.
- [ ] Implement DecisionGenerated v1 → v2 upcaster:
- [ ] Add model_versions{} reconstructed by loading contributing AgentSession streams.
- [ ] Document the performance tradeoff of extra store lookups in `DESIGN.md`.
- [ ] Add the immutability test:
- [ ] Query raw DB payload for a v1 event.
- [ ] Load via EventStore and verify v2 shape.
- [ ] Re-query raw DB payload and verify it is unchanged.
- [ ] Implement cryptographic audit chain for AuditLedger:
- [ ] Hash payloads since last AuditIntegrityCheckRun.
- [ ] new_hash = sha256(previous_hash + event_hashes).
- [ ] Append AuditIntegrityCheckRun with integrity_hash and previous_hash.
- [ ] Detect tampering by verifying chain continuity.
- [ ] Implement reconstruct_agent_context per brief:
- [ ] Load full AgentSession stream.
- [ ] Identify last completed action and pending work.
- [ ] Summarize older events and keep last 3 verbatim.
- [ ] Flag NEEDS_RECONCILIATION if last event is partial/unfinished.
- [ ] Add crash-recovery test proving reconstruction is sufficient to resume.
- [ ] Update `DESIGN.md` with upcasting inference tradeoffs (error rates, null vs inference).

---

## Phase 6 — MCP Server + Docker (branch: `mcp-interface`)
- [ ] Implement all 8 MCP tools with explicit validation and preconditions (from brief):
- [ ] submit_application: schema validation and duplicate application_id check.
- [ ] record_credit_analysis: active AgentSession with context loaded; optimistic concurrency on loan stream.
- [ ] record_fraud_screening: active AgentSession with context loaded; fraud_score in 0.0–1.0.
- [ ] record_compliance_check: rule_id exists in active regulation_set_version.
- [ ] generate_decision: all required analyses present; enforce confidence floor.
- [ ] record_human_review: reviewer_id authentication; if override=True then override_reason required.
- [ ] start_agent_session: writes AgentContextLoaded with context source and token count.
- [ ] run_integrity_check: compliance role only; rate-limited to 1/minute per entity.
- [ ] Ensure all tool errors are structured objects with:
- [ ] error_type, message, stream_id, expected_version, actual_version, suggested_action.
- [ ] Implement MCP resources backed by projections only (no aggregate replay), except:
- [ ] ledger://applications/{id}/audit-trail reads AuditLedger stream directly.
- [ ] ledger://agents/{id}/sessions/{session_id} reads AgentSession stream directly.
- [ ] Implement ledger://ledger/health that returns get_lag() for every projection in <10ms.
- [ ] Add full MCP integration test that runs the complete lifecycle using only MCP tools:
- [ ] start_agent_session → record_credit_analysis → generate_decision → record_human_review → query compliance view.
- [ ] Add Docker support:
- [ ] Dockerfile builds and runs the service.
- [ ] Compose file wires Postgres and service together if used.
- [ ] Provide run instructions in README.
- [ ] Finalize `DESIGN.md` with EventStoreDB concept mapping and “what I’d do differently.”

---

## Bonus — What-If + Regulatory Package (branch: `what-if-regulatory`)
- [ ] Implement what-if projector per brief (never writes to real store):
- [ ] Load real events up to the branch event type.
- [ ] Inject counterfactual events instead of the real branch event.
- [ ] Continue replaying only causally independent real events.
- [ ] Skip real events causally dependent on the branch.
- [ ] Return real_outcome, counterfactual_outcome, divergence_events[].
- [ ] Demonstrate required scenario: change CreditAnalysis risk_tier from MEDIUM to HIGH and show a materially different ApplicationSummary outcome.
- [ ] Implement generate_regulatory_package(application_id, examination_date) that outputs a self-contained JSON file containing:
- [ ] Full ordered event stream with payloads.
- [ ] Projection states as of examination_date.
- [ ] Integrity verification result and hash chain summary.
- [ ] Human-readable narrative (one sentence per significant event).
- [ ] Model versions, confidence scores, and input hashes for all contributing agents.
- [ ] Add tests proving counterfactual divergence and regulatory package completeness.

---

## Test Requirements (apply throughout)
- [ ] Unit tests for EventStore, aggregates, projections, and upcasters.
- [ ] Integration tests for MCP full lifecycle.
- [ ] Concurrency test for expected_version conflict behavior.
- [ ] Immutability test for upcasting.
- [ ] Projection lag SLO tests under simulated load.
- [ ] Gas Town reconstruction test for agent recovery.
- [ ] Integration test for rebuild_from_scratch without downtime.

---

**Assumptions**
- `pytest` is the test framework.
- `ruff` handles linting + formatting.
- `uv` is the only package manager/runner.
- Docker is added at the very end, after MCP is working.
