"""
scripts/run_pipeline.py
=======================
End-to-end pipeline runner.

Usage:
    python scripts/run_pipeline.py --app APEX-0001
    [--phase document|credit|fraud|compliance|decision|all]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def build_deps() -> tuple[asyncpg.Pool, Any, Any]:
    """Build shared store + registry + agent instances."""
    from ledger.infrastructure.store import EventStore
    from ledger.registry.client import ApplicantRegistryClient

    dsn = (
        f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}"
        f"@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    )
    pool = await asyncpg.create_pool(dsn)
    store = EventStore(pool)
    registry = ApplicantRegistryClient(pool)
    return pool, store, registry


async def run_phase(app_id: str, phase: str) -> None:
    pool, store, registry = await build_deps()

    try:
        from ledger.agents.credit_analysis_agent import CreditAnalysisAgent
        from ledger.agents.stub_agents import (
            ComplianceAgent,
            DecisionOrchestratorAgent,
            DocumentProcessingAgent,
            FraudDetectionAgent,
        )

        agents = {
            "document": DocumentProcessingAgent(
                "agent-doc-001", "document_processing", store, registry
            ),
            "credit": CreditAnalysisAgent("agent-credit-001", "credit_analysis", store, registry),
            "fraud": FraudDetectionAgent("agent-fraud-001", "fraud_detection", store, registry),
            "compliance": ComplianceAgent("agent-compliance-001", "compliance", store, registry),
            "decision": DecisionOrchestratorAgent(
                "agent-orch-001", "decision_orchestrator", store, registry
            ),
        }

        sequence = ["document", "credit", "fraud", "compliance", "decision"]

        if phase == "all":
            phases_to_run = sequence
        elif phase in agents:
            phases_to_run = [phase]
        else:
            print(f"Unknown phase: {phase}. Choose from: {', '.join(sequence + ['all'])}")
            return

        for p in phases_to_run:
            print(f"\n{'=' * 60}")
            print(f"Running phase: {p.upper()} for application: {app_id}")
            print(f"{'=' * 60}")
            try:
                await agents[p].process_application(app_id)
                print(f"✓ {p} phase completed successfully")
            except Exception as e:
                print(f"✗ {p} phase failed: {e}")
                raise
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Apex loan processing pipeline")
    parser.add_argument("--app", required=True, help="Application ID (e.g. APEX-0001)")
    parser.add_argument(
        "--phase",
        default="all",
        choices=["document", "credit", "fraud", "compliance", "decision", "all"],
        help="Pipeline phase to run (default: all)",
    )
    args = parser.parse_args()
    asyncio.run(run_phase(args.app, args.phase))


if __name__ == "__main__":
    main()
