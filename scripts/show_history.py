#!/usr/bin/env python3
"""Show me the complete decision history of application ID X.

Demonstration script for TRP1 Challenge Week 5.
Usage: python show_history.py <application_id>
"""

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from ledger.core.regulatory_package import generate_regulatory_package
from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.projections.compliance_audit import ComplianceAuditViewProjection
from ledger.infrastructure.store import EventStore

# ANSI color codes for pretty-printing
BLUE = "\033[94m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


def print_header(title: str) -> None:
    print(f"\n{BOLD}{BLUE}{'=' * 80}{RESET}")
    print(f"{BOLD}{BLUE} {title.center(78)} {RESET}")
    print(f"{BOLD}{BLUE}{'=' * 80}{RESET}\n")


def print_row(label: str, value: Any, color: str = YELLOW) -> None:
    print(f"{BOLD}{label:<25}{RESET} {color}{value}{RESET}")


async def show_history(application_id: str) -> None:
    pool = await get_pool()
    store = EventStore(pool)
    compliance_proj = ComplianceAuditViewProjection(pool)

    print(f"Fetching complete decision history for: {BOLD}{application_id}{RESET}...")

    try:
        # Generate the full regulatory package as of NOW
        now = datetime.now(UTC)
        package = await generate_regulatory_package(
            application_id=application_id,
            examination_date=now,
            store=store,
            compliance_projection=compliance_proj,
        )
    except Exception as e:
        print(f"{RED}Error generating decision history: {e}{RESET}")
        await pool.close()
        return

    # 1. Header & Summary
    print_header(f"DECISION HISTORY: {application_id}")
    summaries = package["projection_states"]["application_summary"]
    print_row("Current State:", summaries.get("state"))
    print_row("Risk Tier:", summaries.get("risk_tier"))
    print_row(
        "Compliance Status:",
        summaries.get("compliance_status"),
        GREEN if summaries.get("compliance_status") == "PASSED" else RED,
    )
    print_row(
        "Final Decision:",
        summaries.get("final_decision"),
        GREEN if summaries.get("final_decision") == "APPROVE" else RED,
    )
    print_row("Checksum (SHA-256):", package["package_checksum"][:16] + "...")

    # 2. Timeline
    print_header("TIMELINE")
    for item in package["narrative"]:
        dt = datetime.fromisoformat(item["recorded_at"])
        ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{YELLOW}{ts}{RESET}] {BOLD}{item['event_type']:<28}{RESET} {item['sentence']}")

    # 3. Agent Attribution
    print_header("AGENT ATTRIBUTION & CAUSAL LINKS")
    for attr in package["agent_attribution"]:
        print(f"• {BOLD}Agent:{RESET} {attr['agent_id']} ({attr['model_version']})")
        print(f"  {BOLD}Action:{RESET} {attr['event_type']}")
        if attr.get("confidence_score") is not None:
            print(f"  {BOLD}Confidence:{RESET} {attr['confidence_score']:.2f}")
        if attr.get("input_data_hash"):
            print(f"  {BOLD}Input Hash:{RESET} {attr['input_data_hash'][:16]}...")
        if attr.get("recommendation"):
            print(f"  {BOLD}Recommendation:{RESET} {attr['recommendation']}")
        print()

    # 4. Integrity Check
    print_header("CRYPTOGRAPHIC INTEGRITY")
    integrity = package["integrity_verification"]
    status_color = GREEN if integrity["is_valid"] else RED
    print_row("Chain Status:", "VALID" if integrity["is_valid"] else "TAMPERED", status_color)
    print_row("Verification Msg:", integrity["message"], status_color)

    print(f"\n{BOLD}{BLUE}{'=' * 80}{RESET}\n")

    await pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python show_history.py <application_id>")
        sys.exit(1)

    app_id = sys.argv[1].removeprefix("loan-")
    asyncio.run(show_history(app_id))
