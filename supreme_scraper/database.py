"""
database.py — Async SQLAlchemy engine, session factory, and schema bootstrap.

SECURITY:
- echo=False prevents SQL statements from appearing in log output.
- WAL journal mode is set on connect for better SQLite concurrency.
- create_all() is used for schema management (no Alembic in this prototype);
  document the migration path in README.md for production use.
"""

from __future__ import annotations

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from supreme_scraper.config import settings
from supreme_scraper.logging_config import get_logger
from supreme_scraper.models import Base

logger = get_logger(__name__)

# echo=False — no SQL in logs (prevents accidental data leakage)
_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
)

AsyncSessionFactory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    _engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def init_db() -> None:
    """
    Create all tables if they don't exist. Call once at startup.

    Also enables WAL journal mode for better concurrency under the
    APScheduler single-instance constraint (max_instances=1).
    """
    async with _engine.begin() as conn:
        # SQLite WAL mode: readers don't block writers
        await conn.execute(text("PRAGMA journal_mode=WAL"))
        await conn.run_sync(Base.metadata.create_all)

    logger.info("database.initialized", url=settings.DATABASE_URL)
