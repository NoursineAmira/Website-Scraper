"""
demo.py — End-to-end pipeline demonstration.

Simulates a full scrape cycle without needing the APScheduler daemon.
Shows every pipeline stage with structured log output.

Usage:
    python demo.py               # Live HTTP fetch from supreme.com
    python demo.py --fixture     # Offline demo using tests/fixtures/supreme_preview_all.html
    python demo.py --fixture --twice  # Run twice to demonstrate change detection
                                      # (second run marks first batch as 'removed')

Flow per run:
    trigger → fetch (or fixture) → parse → upsert → detect removals
    → alert (mock or Telegram) → audit log (always, in finally)
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from pathlib import Path

from supreme_scraper.alerting import AlertService
from supreme_scraper.config import settings
from supreme_scraper.crawler import Crawler, CrawlDisallowedError
from supreme_scraper.database import AsyncSessionFactory, init_db
from supreme_scraper.logging_config import configure_logging, get_logger
from supreme_scraper.parser import parse_preview_page
from supreme_scraper.store import append_scrape_log, mark_removed_products, upsert_drops

logger = get_logger("demo")


async def run_demo(use_fixture: bool = False, twice: bool = False) -> None:
    configure_logging()
    await init_db()

    runs = 2 if (use_fixture and twice) else 1

    for run_num in range(1, runs + 1):
        if runs > 1:
            logger.info("demo.run_start", run=run_num, total=runs)

        scraped_at = datetime.now(timezone.utc)
        status_code: int | None = None
        duration_ms: int | None = None
        records_upserted = 0
        error_msg: str | None = None

        try:
            # ---- STEP 1: FETCH ----------------------------------------
            if use_fixture:
                fixture_path = (
                    Path(__file__).parent / "tests" / "fixtures" / "supreme_preview_all.html"
                )
                html = fixture_path.read_text(encoding="utf-8")
                status_code = 200
                duration_ms = 0
                source_url = settings.TARGET_URL
                logger.info("demo.fixture_loaded", path=str(fixture_path))
            else:
                async with Crawler() as crawler:
                    result = await crawler.fetch(settings.TARGET_URL)
                html = result.html
                status_code = result.status_code
                duration_ms = result.duration_ms
                source_url = result.url
                logger.info(
                    "demo.fetch_complete",
                    status_code=status_code,
                    duration_ms=duration_ms,
                )

            # ---- STEP 2: PARSE ----------------------------------------
            records = parse_preview_page(html, source_url)
            logger.info("demo.parse_complete", record_count=len(records))

            # ---- STEP 3: UPSERT + CHANGE DETECTION -------------------
            async with AsyncSessionFactory() as session:
                records_upserted, upsert_changes = await upsert_drops(session, records)
                seen_urls = {r["product_url"] for r in records}

                # On the second run (--twice), simulate half the products disappearing
                if twice and run_num == 2:
                    seen_urls = set(list(seen_urls)[:1])  # pretend only 1 survived

                removal_changes = await mark_removed_products(
                    session, seen_urls, settings.SOURCE_WEBSITE
                )

            all_changes = upsert_changes + removal_changes
            logger.info(
                "demo.upsert_complete",
                upserted=records_upserted,
                status_changes=len(upsert_changes),
                removals=len(removal_changes),
            )

            # ---- STEP 4: ALERT ----------------------------------------
            if all_changes:
                alert_svc = AlertService()
                await alert_svc.send_bulk_alerts(all_changes)
                logger.info("demo.alerts_sent", count=len(all_changes))
            else:
                logger.info("demo.no_changes_detected")

        except CrawlDisallowedError as exc:
            error_msg = f"robots.txt disallowed: {exc}"
            logger.warning("demo.crawl_disallowed", error=error_msg)

        except Exception as exc:
            error_msg = str(exc)
            logger.error("demo.failed", error=error_msg)
            raise

        finally:
            # ---- STEP 5: AUDIT LOG (always written) -------------------
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
            logger.info("demo.audit_log_written")

    logger.info("demo.complete")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Supreme drop scraper — end-to-end demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Use fixture HTML instead of a live HTTP fetch (offline mode)",
    )
    parser.add_argument(
        "--twice",
        action="store_true",
        help="Run two cycles to demonstrate removal change detection (requires --fixture)",
    )
    args = parser.parse_args()

    asyncio.run(run_demo(use_fixture=args.fixture, twice=args.twice))
