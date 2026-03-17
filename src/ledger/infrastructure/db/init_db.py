import asyncio
from pathlib import Path

from ledger.infrastructure.db.connection import get_connection


async def initialize_database() -> None:
    """Initializes the database with the event store schema."""
    conn = await get_connection()
    try:
        schema_path = Path(__file__).parent / "schema.sql"
        with schema_path.open() as f:
            schema_sql = f.read()

        print("Initializing database schema...")
        await conn.execute(schema_sql)
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")
        raise
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(initialize_database())
