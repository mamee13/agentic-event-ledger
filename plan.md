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
- [ ] Implement LoanApplicationAggregate with explicit apply handlers for each relevant event.
- [ ] Implement AgentSessionAggregate with explicit apply handlers for each relevant event.
- [ ] Ensure aggregate `load()` replays stream events in order and updates `version` to the last event position.
- [ ] Implement command handlers that follow: load aggregates -> validate rules -> build events -> append with expected_version.
- [ ] Enforce the valid state transition sequence exactly as specified in the brief.
- [ ] Enforce that AgentContextLoaded must be the first event in every AgentSession stream.
- [ ] Enforce model-version locking so a second credit analysis is rejected unless superseded by human override.
- [ ] Enforce the confidence floor so DecisionGenerated with confidence < 0.6 is forced to REFER.
- [ ] Enforce compliance dependency so ApplicationApproved cannot be appended unless required checks have passed.
- [ ] Enforce causal chain rules so contributing_agent_sessions reference only valid sessions for the application.
- [ ] Add unit tests for each rule, including negative tests for missing prerequisites.
- [ ] Add tests for invalid transition attempts (e.g., Approved -> UnderReview).
- [ ] Update `DESIGN.md` with aggregate boundary reasoning and coupling tradeoffs.

---

## Phase 4 — Projections + Daemon (branch: `projections-daemon`)
- [ ] Implement ProjectionDaemon with:
- [ ] Continuous polling loop that loads from the lowest checkpoint.
- [ ] Routing of each event to subscribed projections.
- [ ] Checkpoint updates after successful batch processing.
- [ ] Fault tolerance that logs errors, retries a limited number of times, then skips the offending event.
- [ ] Lag measurement per projection based on global_position difference.
- [ ] Build ApplicationSummary projection:
- [ ] Store current application state fields exactly as specified.
- [ ] Update on every relevant event and track last_event_type and last_event_at.
- [ ] Build AgentPerformanceLedger projection:
- [ ] Aggregate counts and averages by agent_id and model_version.
- [ ] Update rates (approve/decline/refer) as decisions are generated.
- [ ] Build ComplianceAuditView projection:
- [ ] Store a full compliance record per application with rule versions and evidence hashes.
- [ ] Implement get_current_compliance for live view queries.
- [ ] Implement get_compliance_at(timestamp) using your snapshot strategy.
- [ ] Implement rebuild_from_scratch that replays all events without downtime to reads.
- [ ] Define and document the snapshot strategy in `DESIGN.md`.
- [ ] Add tests simulating concurrent command load and validate lag SLOs (<500ms ApplicationSummary, <2s ComplianceAuditView).

---

## Phase 5 — Upcasting + Integrity + Gas Town (branch: `upcasting-integrity`)
- [ ] Implement UpcasterRegistry with auto-registration and chained upcasting by version.
- [ ] Implement CreditAnalysisCompleted v1 -> v2 upcaster with:
- [ ] model_version inferred from recorded_at or a documented rule.
- [ ] confidence_score set to null when truly unknown.
- [ ] regulatory_basis inferred from rule set active at recorded_at.
- [ ] Implement DecisionGenerated v1 -> v2 upcaster with:
- [ ] model_versions reconstructed from contributing agent sessions.
- [ ] Performance implications documented in `DESIGN.md`.
- [ ] Add immutability test:
- [ ] Raw DB payload remains unchanged after upcasted reads.
- [ ] Loaded events are upcasted to latest version in memory only.
- [ ] Implement cryptographic audit chain:
- [ ] Hash all event payloads since last integrity check.
- [ ] Combine with previous hash and store in AuditIntegrityCheckRun.
- [ ] Detect tampering by verifying hash chain continuity.
- [ ] Implement reconstruct_agent_context:
- [ ] Load full AgentSession stream and identify last action.
- [ ] Summarize older events while keeping last 3 verbatim.
- [ ] Flag NEEDS_RECONCILIATION if last event indicates a partial decision.
- [ ] Add crash-recovery test that verifies context reconstruction is sufficient for resumption.
- [ ] Update `DESIGN.md` with upcasting inference tradeoffs, error rates, and null vs inference decisions.

---

## Phase 6 — MCP Server + Docker (branch: `mcp-interface`)
- [ ] Implement all 8 MCP tools with explicit validation and preconditions:
- [ ] submit_application validates schema and duplicate IDs.
- [ ] record_credit_analysis requires active AgentSession with context loaded.
- [ ] record_fraud_screening enforces fraud_score range 0.0–1.0.
- [ ] record_compliance_check ensures rule_id exists in regulation set.
- [ ] generate_decision validates all prerequisites and enforces confidence floor.
- [ ] record_human_review requires reviewer auth and override_reason if override.
- [ ] start_agent_session writes AgentContextLoaded and returns session context position.
- [ ] run_integrity_check enforces compliance role and rate limit.
- [ ] Ensure all tool errors are structured with error_type, message, expected/actual versions where relevant, and suggested_action.
- [ ] Implement MCP resources backed by projections (no aggregate replay), except for explicitly allowed stream reads.
- [ ] Implement ledger health resource that returns all projection lags quickly.
- [ ] Add full MCP integration test that runs the entire application lifecycle using only MCP tools.
- [ ] Add Docker support:
- [ ] Dockerfile builds and runs the service.
- [ ] Compose file (if used) wires Postgres and service together.
- [ ] Provide clear run instructions in README.
- [ ] Finalize `DESIGN.md` with EventStoreDB concept mapping and “what I’d do differently.”

---

## Bonus — What-If + Regulatory Package (branch: `what-if-regulatory`)
- [ ] Implement what-if projector that branches at an event type without writing to the real store.
- [ ] Load real events up to the branch point, inject counterfactual events, then continue with only causally independent real events.
- [ ] Demonstrate the required scenario where changing risk tier produces a materially different outcome.
- [ ] Implement generate_regulatory_package to output a self-contained JSON file containing:
- [ ] Full ordered event stream with payloads.
- [ ] Projection states as of examination date.
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
