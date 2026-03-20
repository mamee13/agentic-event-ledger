# Agentic Event Ledger

An event-sourced ledger system designed for AI agents and enterprise auditing.

## Development Setup

### Prerequisites
- Python 3.11+
- [uv](https://docs.astral.sh/uv/)

> [!IMPORTANT]
> **Tooling Enforcement**: All tooling (install, run, test, lint) **must** be run via `uv`. Do not use `pip`, `poetry`, or other package managers.

### Installation
1. Clone the repository and navigate to the root:
   ```bash
   git clone <repo-url>
   cd agentic-event-ledger
   ```
2. Set up the environment and install dependencies:
   ```bash
   uv sync
   ```
3. Configure environment variables:
   ```bash
   cp .env.example .env
   # Edit .env with your OpenRouter API key and desired model
   ```
4. Install pre-commit hooks:
   ```bash
   uv run pre-commit install
   ```

### Running Migrations

Apply the schema to your Postgres database using `psql`:
```bash
psql $DATABASE_URL -f src/ledger/infrastructure/db/schema.sql
```

Or if you prefer to pass credentials explicitly:
```bash
psql -h localhost -U postgres -d ledger -f src/ledger/infrastructure/db/schema.sql
```

The `DATABASE_URL` variable is set in your `.env` file (see `.env.example`). The schema is idempotent — safe to re-run.

### Workflow Commands
- **Run Tests**: `uv run pytest`
- **Linting**: `uv run ruff check . --fix`
- **Formatting**: `uv run ruff format .`
- **Type Checking**: `uv run mypy .`
- **Run Application**: `uv run ledger` (once implemented)

## Running the MCP Server

### Locally (requires Postgres running)
```bash
uv run python main.py
```

### With Docker Compose (Postgres + server wired together)
```bash
# Copy and fill in your env
cp .env.example .env

# Build and start
docker compose up --build

# Apply schema (first run only — Compose mounts it automatically via initdb)
# If running against an existing DB:
psql $DATABASE_URL -f src/ledger/infrastructure/db/schema.sql
```

The MCP server listens on port **8000**. Connect any MCP-compatible client to it.

### Available MCP Tools
| Tool | Description |
|---|---|
| `submit_application` | Submit a new loan application |
| `start_agent_session` | Start an agent session (writes AgentContextLoaded) |
| `record_credit_analysis` | Record a completed credit analysis |
| `record_fraud_screening` | Record a fraud screening result |
| `record_compliance_check` | Record a compliance rule result |
| `generate_decision` | Generate a loan decision |
| `record_human_review` | Record a human reviewer's decision |
| `run_integrity_check` | Run cryptographic audit chain check (COMPLIANCE_OFFICER only) |

### Available MCP Resources
| URI | Backed by |
|---|---|
| `ledger://applications/{id}` | ApplicationSummary projection |
| `ledger://applications/{id}/audit-trail` | AuditLedger stream (direct) |
| `ledger://applications/{id}/compliance` | ComplianceAuditView projection |
| `ledger://agents/{id}/sessions/{session_id}` | AgentSession stream (direct) |
| `ledger://agents/{id}/performance` | AgentPerformance projection |
| `ledger://ledger/health` | Projection lag for all projections |

## Architecture Overview
See [DESIGN.md](DESIGN.md) for details on the event store, aggregates, and projections.