import asyncio
import os
import sys
from pathlib import Path

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import asyncpg
from dotenv import load_dotenv

load_dotenv()


async def seed() -> None:
    dsn = f"postgresql://{os.environ['POSTGRES_USER']}:{os.environ['POSTGRES_PASSWORD']}@{os.environ['POSTGRES_HOST']}:{os.environ['POSTGRES_PORT']}/{os.environ['POSTGRES_DB']}"
    pool = await asyncpg.create_pool(dsn)

    profiles = [
        ("user-101", "John Q. Public", "Professional Services", "IL", "Individual", "LOW"),
        ("entity-fraud-99", "FastCash Solutions LLC", "Finance", "KY", "LLC", "HIGH"),
        ("user-blocked-404", "Anonymous Trading Group", "Trading", "KY", "Entity", "MEDIUM"),
    ]

    print(f"Seeding {len(profiles)} profiles into applicant_registry...")
    try:
        for pid, name, ind, jur, ltype, risk in profiles:
            await pool.execute(
                """
                INSERT INTO applicant_registry.companies
                 (company_id, name, industry, naics, jurisdiction, legal_type, founded_year,
                  employee_count,
                  ein, address_city, address_state, relationship_start, account_manager,
                  risk_segment, trajectory, submission_channel, ip_region)
                VALUES
                 ($1, $2, $3, '000000', $4, $5, 2020, 1,
                  $6, 'Unknown', 'Unknown', NOW(), 'SYSTEM',
                  $7, 'STEADY', 'WEB', 'US')
                ON CONFLICT (company_id) DO NOTHING
                """,
                pid,
                name,
                ind,
                jur,
                ltype,
                f"EIN-{pid}",
                risk,
            )
            print(f"  ✓ {pid} ({name})")
        print("Done!")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(seed())
