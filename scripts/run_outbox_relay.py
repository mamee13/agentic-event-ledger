#!/usr/bin/env python3
"""Run the OutboxRelay for demo purposes.

Usage:
  uv run python scripts/run_outbox_relay.py
"""

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ledger.infrastructure.db.connection import get_pool
from ledger.infrastructure.outbox import OutboxRelay

logging.basicConfig(level=logging.INFO)


async def _publish_stub(message: dict[str, Any]) -> None:
    # Demo publisher: logs the payload only.
    logging.info("Outbox publish -> %s", message)


async def main() -> None:
    pool = await get_pool()
    relay = OutboxRelay(pool, publish=_publish_stub, batch_size=100, poll_interval_ms=250)
    try:
        await relay.run_forever()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
