"""
test_store.py — Unit tests for store.py using in-memory SQLite.

No network calls. All tests use an isolated async in-memory database so
they cannot interfere with each other or with any real DB file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from supreme_scraper.models import Base, Drop, ScrapeLog
from supreme_scraper.store import (
    StockStatusChange,
    append_scrape_log,
    mark_removed_products,
    upsert_drops,
)

_TEST_DB = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def session():
    """Fresh in-memory SQLite database per test."""
    engine = create_async_engine(_TEST_DB, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


def _make_record(
    name: str = "Test Box Logo Tee",
    stock: str = "in_preview",
    slug: str = "test-box-logo-tee",
    source: str = "supreme.com",
) -> dict:
    return {
        "product_name": name,
        "brand": "Supreme",
        "sku": "aaaaaaaa-0000-0000-0000-000000000001",
        "colorway": "Tops/Sweaters",
        "retail_price": None,
        "currency": "USD",
        "drop_date": "Spring/Summer 2026 Preview",
        "drop_method": "online",
        "stock_status": stock,
        "available_sizes": json.dumps([]),
        "product_url": f"https://supreme.com/previews/springsummer2026/{slug}",
        "image_url": None,
        "source_website": source,
        "scrape_timestamp": datetime.now(timezone.utc),
        "resale_low": None,
        "resale_high": None,
        "notes": "White",
    }


# ------------------------------------------------------------------ #
# upsert_drops                                                         #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_insert_new_record(session):
    count, changes = await upsert_drops(session, [_make_record()])
    assert count == 1
    assert changes == []


@pytest.mark.asyncio
async def test_upsert_does_not_duplicate(session):
    record = _make_record()
    await upsert_drops(session, [record])
    await upsert_drops(session, [record])

    result = await session.execute(select(func.count()).select_from(Drop))
    assert result.scalar() == 1


@pytest.mark.asyncio
async def test_stock_status_change_detected(session):
    await upsert_drops(session, [_make_record(stock="in_preview")])
    _, changes = await upsert_drops(session, [_make_record(stock="removed")])

    assert len(changes) == 1
    change = changes[0]
    assert isinstance(change, StockStatusChange)
    assert change.old_status == "in_preview"
    assert change.new_status == "removed"
    assert "Test Box Logo Tee" in change.product_name


@pytest.mark.asyncio
async def test_no_change_emits_no_alert(session):
    await upsert_drops(session, [_make_record(stock="in_preview")])
    _, changes = await upsert_drops(session, [_make_record(stock="in_preview")])
    assert changes == []


@pytest.mark.asyncio
async def test_multiple_records_inserted(session):
    records = [
        _make_record(name="Product A", slug="product-a"),
        _make_record(name="Product B", slug="product-b"),
        _make_record(name="Product C", slug="product-c"),
    ]
    count, changes = await upsert_drops(session, records)
    assert count == 3
    assert changes == []

    result = await session.execute(select(func.count()).select_from(Drop))
    assert result.scalar() == 3


# ------------------------------------------------------------------ #
# mark_removed_products                                                #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_mark_removed_when_url_absent(session):
    record = _make_record()
    await upsert_drops(session, [record])

    # Second scrape sees an empty set — product has disappeared
    changes = await mark_removed_products(session, set(), "supreme.com")

    assert len(changes) == 1
    assert changes[0].new_status == "removed"


@pytest.mark.asyncio
async def test_no_removal_when_url_present(session):
    record = _make_record()
    await upsert_drops(session, [record])

    seen = {record["product_url"]}
    changes = await mark_removed_products(session, seen, "supreme.com")
    assert changes == []


@pytest.mark.asyncio
async def test_already_removed_not_re_emitted(session):
    record = _make_record(stock="removed")
    await upsert_drops(session, [record])

    # Already removed — mark_removed_products should not emit another change
    changes = await mark_removed_products(session, set(), "supreme.com")
    assert changes == []


# ------------------------------------------------------------------ #
# append_scrape_log                                                    #
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_scrape_log_appended(session):
    await append_scrape_log(
        session=session,
        url="https://supreme.com/previews/springsummer2026/all",
        status_code=200,
        scraped_at=datetime.now(timezone.utc),
        duration_ms=342,
        records_upserted=5,
        error=None,
    )
    result = await session.execute(select(ScrapeLog))
    logs = list(result.scalars().all())
    assert len(logs) == 1
    assert logs[0].status_code == 200
    assert logs[0].records_upserted == 5
    assert logs[0].error is None


@pytest.mark.asyncio
async def test_scrape_log_error_truncated(session):
    long_error = "E" * 5000
    await append_scrape_log(
        session=session,
        url="https://supreme.com/previews/springsummer2026/all",
        status_code=500,
        scraped_at=datetime.now(timezone.utc),
        duration_ms=0,
        records_upserted=0,
        error=long_error,
    )
    result = await session.execute(select(ScrapeLog))
    log = result.scalars().first()
    assert log is not None
    assert len(log.error) <= 2048


@pytest.mark.asyncio
async def test_scrape_log_is_append_only(session):
    """Verify we can insert multiple log rows but never update them."""
    for i in range(3):
        await append_scrape_log(
            session=session,
            url="https://supreme.com/previews/springsummer2026/all",
            status_code=200,
            scraped_at=datetime.now(timezone.utc),
            duration_ms=i * 100,
            records_upserted=i,
            error=None,
        )
    result = await session.execute(select(func.count()).select_from(ScrapeLog))
    assert result.scalar() == 3
