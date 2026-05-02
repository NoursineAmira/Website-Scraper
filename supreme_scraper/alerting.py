"""
alerting.py — Stock status change notifications via Discord Webhook or mock log.

SECURITY:
- The Discord webhook URL is read from settings (environment variable only).
- All exceptions from the Discord send are caught — alerting failure never
  propagates to the scheduler.
"""

from __future__ import annotations

import asyncio

import httpx

from supreme_scraper.config import settings
from supreme_scraper.logging_config import get_logger
from supreme_scraper.store import StockStatusChange

logger = get_logger(__name__)


def _format_alert(change: StockStatusChange) -> str:
    if change.old_status == "not_listed" and change.new_status == "in_preview":
        label = "🆕 BRAND NEW PRODUCT ADDED"
    elif change.new_status == "in_preview":
        label = "🟢 BACK IN PREVIEW"
    elif change.new_status == "removed":
        label = "🔴 REMOVED FROM PREVIEW"
    else:
        label = f"⚪ STATUS: {change.new_status.upper()}"

    return (
        f"**[Supreme Drop Alert]**\n"
        f"{label}\n\n"
        f"**Product:** {change.product_name}\n"
        f"**Was:** {change.old_status}  →  **Now:** {change.new_status}\n"
        f"**URL:** {change.product_url}"
    )


class AlertService:
    def __init__(self) -> None:
        self._webhook_url = settings.DISCORD_WEBHOOK_URL
        self._enabled = bool(self._webhook_url)

        if not self._enabled:
            logger.info(
                "alerting.mock_mode",
                reason="DISCORD_WEBHOOK_URL not configured",
            )

    async def send_change_alert(self, change: StockStatusChange) -> None:
        message = _format_alert(change)

        logger.info(
            "alerting.change_detected",
            product=change.product_name,
            old_status=change.old_status,
            new_status=change.new_status,
        )

        if not self._enabled:
            logger.info("alerting.mock_send", message=message)
            return

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    self._webhook_url,
                    json={"content": message},
                    timeout=10.0,
                )
                response.raise_for_status()
            logger.info("alerting.sent", product=change.product_name)

        except Exception as exc:
            logger.error(
                "alerting.send_failed",
                product=change.product_name,
                error=str(exc),
            )

    async def send_bulk_alerts(self, changes: list[StockStatusChange]) -> None:
        for i, change in enumerate(changes):
            await self.send_change_alert(change)
            if i < len(changes) - 1:
                await asyncio.sleep(1.0)