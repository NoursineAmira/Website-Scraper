"""
store.py — Database upsert, change detection, and audit log.

SECURITY:
- All queries use SQLAlchemy ORM with bound parameters. No string-formatted
  SQL exists anywhere in this module — SQL injection is structurally prevented.
- ScrapeLog rows are INSERT-only. No UPDATE path exists for that table.
- Error messages written to ScrapeLog.error are truncated to 2048 chars
  to prevent large-payload injection into the audit trail.
- upsert_drops writes only to named ORM model attributes, never via **kwargs
  applied directly to a model from untrusted input.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from supreme_scraper.logging_config import get_logger
from supreme_scraper.models import Drop, ScrapeLog

logger = get_logger(__name__)

_MAX_ERROR_LEN = 2048


class StockStatusChange:
    """
    Value object representing a detected stock_status transition.
    Emitted by upsert_drops and mark_removed_products.
    """

    __slots__ = ("product_name", "product_url", "old_status", "new_status", "drop_id")

    def __init__(
        self,
        product_name: str,
        product_url: str,
        old_status: str,
        new_status: str,
        drop_id: int,
    ) -> None:
        self.product_name = product_name
        self.product_url = product_url
        self.old_status = old_status
        self.new_status = new_status
        self.drop_id = drop_id

    def __repr__(self) -> str:
        return (
            f"StockStatusChange(product={self.product_name!r}, "
            f"{self.old_status!r} -> {self.new_status!r})"
        )


async def upsert_drops(
    session: AsyncSession,
    records: list[dict[str, Any]],
) -> tuple[int, list[StockStatusChange]]:
    """
    Upsert parsed product records into the drops table.

    Algorithm:
      For each record, query by (product_url, source_website).
        If found:  detect stock_status change before overwriting mutable fields.
        If not found: insert a new Drop row.

    Returns:
        (upserted_count, list_of_StockStatusChange)
    """
    changes: list[StockStatusChange] = []
    upserted = 0

    for record in records:
        try:
            result = await session.execute(
                select(Drop).where(
                    Drop.product_url == record["product_url"],
                    Drop.source_website == record["source_website"],
                )
            )
            existing: Drop | None = result.scalars().one_or_none()

            if existing is not None:
                old_status = existing.stock_status
                new_status = record["stock_status"]

                if old_status != new_status:
                    changes.append(
                        StockStatusChange(
                            product_name=existing.product_name,
                            product_url=existing.product_url,
                            old_status=old_status,
                            new_status=new_status,
                            drop_id=existing.id,
                        )
                    )
                    logger.info(
                        "store.stock_status_changed",
                        product=existing.product_name,
                        old=old_status,
                        new=new_status,
                        drop_id=existing.id,
                    )

                # Only update mutable fields — id and unique key never touched
                existing.product_name = record["product_name"]
                existing.colorway = record.get("colorway")
                existing.drop_date = record.get("drop_date")
                existing.stock_status = new_status
                existing.available_sizes = record.get("available_sizes")
                existing.image_url = record.get("image_url")
                existing.notes = record.get("notes")
                existing.scrape_timestamp = record["scrape_timestamp"]

            else:
                new_drop = Drop(**record)
                session.add(new_drop)
                await session.flush()  # gets the new ID from DB
                changes.append(
                    StockStatusChange(
                        product_name=record["product_name"],
                        product_url=record["product_url"],
                        old_status="not_listed",
                        new_status="in_preview",
                        drop_id=new_drop.id,
                    )
                )

            upserted += 1

        except Exception as exc:
            logger.error(
                "store.upsert_error",
                url=record.get("product_url"),
                error=str(exc),
            )

    await session.commit()
    logger.info(
        "store.upsert_complete",
        count=upserted,
        changes=len(changes),
    )
    return upserted, changes


async def mark_removed_products(
    session: AsyncSession,
    seen_urls: set[str],
    source_website: str,
) -> list[StockStatusChange]:
    """
    Mark products that were previously in the DB but absent from the current
    scrape as 'removed'. This detects products that disappeared from the preview.

    Returns a list of StockStatusChange objects for any newly removed products.
    """
    result = await session.execute(
        select(Drop).where(
            Drop.source_website == source_website,
            Drop.stock_status != "removed",
        )
    )
    existing_rows: list[Drop] = list(result.scalars().all())

    changes: list[StockStatusChange] = []
    now = datetime.now(timezone.utc)

    for row in existing_rows:
        if row.product_url not in seen_urls:
            old_status = row.stock_status
            row.stock_status = "removed"
            row.scrape_timestamp = now
            changes.append(
                StockStatusChange(
                    product_name=row.product_name,
                    product_url=row.product_url,
                    old_status=old_status,
                    new_status="removed",
                    drop_id=row.id,
                )
            )
            logger.info(
                "store.product_removed_from_preview",
                product=row.product_name,
                drop_id=row.id,
            )

    if changes:
        await session.commit()
        logger.info("store.removed_products_committed", count=len(changes))

    return changes


async def append_scrape_log(
    session: AsyncSession,
    url: str,
    status_code: int | None,
    scraped_at: datetime,
    duration_ms: int | None,
    records_upserted: int,
    error: str | None,
    target_id: int | None = None,
) -> None:
    """
    Append one audit row to scrape_log. This is the ONLY write path for
    that table — no UPDATE is ever issued against ScrapeLog.
    """
    log_entry = ScrapeLog(
        target_id=target_id,
        url=url,
        status_code=status_code,
        scraped_at=scraped_at,
        duration_ms=duration_ms,
        records_upserted=records_upserted,
        # Truncate error to prevent large payloads entering the audit trail
        error=error[:_MAX_ERROR_LEN] if error else None,
    )
    session.add(log_entry)
    await session.commit()
    logger.debug("store.scrape_log_appended", url=url, status_code=status_code)
