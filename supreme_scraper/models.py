"""
models.py — SQLAlchemy 2.x ORM models for the two database tables.

Schema is created via create_all() at startup — no Alembic migrations.

SECURITY:
- All String columns carry explicit length limits (data minimization).
- ScrapeLog is append-only by design; no UPDATE path exists anywhere in the
  codebase that targets this table.
- `available_sizes` stores a JSON-serialized list (validated in parser.py),
  never raw HTML text.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Drop(Base):
    """
    One row per unique (product_url, source_website) combination.
    Upserted on every successful scrape cycle.
    stock_status transitions trigger change-detection alerts.
    """

    __tablename__ = "drops"
    __table_args__ = (
        UniqueConstraint("product_url", "source_website", name="uq_drop_url_source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    product_name: Mapped[str] = mapped_column(String(256), nullable=False)
    brand: Mapped[str] = mapped_column(String(128), nullable=False, default="Supreme")
    sku: Mapped[str | None] = mapped_column(String(128), nullable=True)
    colorway: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retail_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    drop_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    drop_method: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Constrained to {"in_preview", "removed", "unknown"} by parser.py
    stock_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="unknown"
    )
    # JSON-serialized list, e.g. '[]' or '["S", "M", "L"]'
    available_sizes: Mapped[str | None] = mapped_column(Text, nullable=True)
    product_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    source_website: Mapped[str] = mapped_column(String(128), nullable=False)
    scrape_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    resale_low: Mapped[float | None] = mapped_column(Float, nullable=True)
    resale_high: Mapped[float | None] = mapped_column(Float, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScrapeLog(Base):
    """
    Append-only audit trail. One row per HTTP fetch attempt.

    SECURITY: This table is never updated after insert. The store.py module
    enforces this by only ever calling session.add(ScrapeLog(...)) — no
    UPDATE statements are ever issued against this table. The `finally` block
    in scheduler.py and demo.py ensures a row is written even on failure.
    """

    __tablename__ = "scrape_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Drop.id if the log entry is associated with a specific product; None for batch runs
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scraped_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    records_upserted: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # Truncated to 2048 chars in store.append_scrape_log to prevent large payloads
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
