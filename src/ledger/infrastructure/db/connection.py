import asyncpg
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    postgres_db: str = "event_ledger"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = DatabaseSettings()


def get_dsn() -> str:
    return f"postgresql://{settings.postgres_user}:{settings.postgres_password}@{settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db}"


async def get_connection() -> asyncpg.Connection:
    return await asyncpg.connect(get_dsn())


async def get_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(get_dsn())
