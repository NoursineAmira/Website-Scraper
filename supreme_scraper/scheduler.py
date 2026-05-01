"""
scheduler.py — APScheduler wiring for the full scrape pipeline.

Runs scrape_job() every SCRAPE_INTERVAL_MINUTES.
max_instances=1 prevents overlapping runs and DB race conditions.

The ScrapeLog audit entry is written in a `finally` block — it is appended
even when the job raises an exception, preserving the audit trail.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from supreme_scraper.alerting import AlertService
from supreme_scraper.config import settings
from supreme_scraper.crawler import Crawler, CrawlDisallowedError
from supreme_scraper.database import AsyncSessionFactory, init_db
from supreme_scraper.logging_config import configure_logging, get_logger
from supreme_scraper.parser import parse_preview_page
from supreme_scraper.store import append_scrape_log, mark_removed_products, upsert_drops

logger = get_logger(__name__)

_alert_service = AlertService()


async def scrape_job() -> None:
    """
    Full pipeline for one scrape cycle:
      crawl → parse → upsert → mark_removed → alert → audit log (always)
    """
    scraped_at = datetime.now(timezone.utc)
    status_code: int | None = None
    duration_ms: int | None = None
    records_upserted = 0
    error_msg: str | None = None

    try:
        async with Crawler() as crawler:
            fetch_result = await crawler.fetch(settings.TARGET_URL)

        status_code = fetch_result.status_code
        duration_ms = fetch_result.duration_ms

        records = parse_preview_page(fetch_result.html, fetch_result.url)

        async with AsyncSessionFactory() as session:
            records_upserted, upsert_changes = await upsert_drops(session, records)
            seen_urls = {r["product_url"] for r in records}
            removal_changes = await mark_removed_products(
                session, seen_urls, settings.SOURCE_WEBSITE
            )

        all_changes = upsert_changes + removal_changes
        if all_changes:
            await _alert_service.send_bulk_alerts(all_changes)

    except CrawlDisallowedError as exc:
        error_msg = f"robots.txt disallowed: {exc}"
        logger.warning("scheduler.crawl_disallowed", error=error_msg)

    except Exception as exc:
        error_msg = str(exc)
        logger.error("scheduler.job_failed", error=error_msg)

    finally:
        # SECURITY / RELIABILITY: audit log is ALWAYS written, even on failure.
        async with AsyncSessionFactory() as session:
            await append_scrape_log(
                session=session,
                url=settings.TARGET_URL,
                status_code=status_code,
                scraped_at=scraped_at,
                duration_ms=duration_ms,
                records_upserted=records_upserted,
                error=error_msg,
            )


async def run_scheduler() -> None:
    """Start the APScheduler and run until interrupted with Ctrl-C."""
    configure_logging()
    await init_db()

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scrape_job,
        trigger="interval",
        minutes=settings.SCRAPE_INTERVAL_MINUTES,
        id="supreme_scrape",
        max_instances=1,        # prevent overlapping DB writes
        misfire_grace_time=60,  # tolerate up to 60s of startup delay
    )
    scheduler.start()

    logger.info(
        "scheduler.started",
        interval_minutes=settings.SCRAPE_INTERVAL_MINUTES,
        target=settings.TARGET_URL,
    )

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown(wait=True)
        logger.info("scheduler.shutdown")


if __name__ == "__main__":
    asyncio.run(run_scheduler())
