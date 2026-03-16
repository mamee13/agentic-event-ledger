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
   # Edit .env with your local settings
   ```
4. Install pre-commit hooks:
   ```bash
   uv run pre-commit install
   ```

### Workflow Commands
- **Run Tests**: `uv run pytest`
- **Linting**: `uv run ruff check . --fix`
- **Formatting**: `uv run ruff format .`
- **Type Checking**: `uv run mypy .`
- **Run Application**: `uv run ledger` (once implemented)

## Architecture Overview
See [DESIGN.md](DESIGN.md) for details on the event store, aggregates, and projections.