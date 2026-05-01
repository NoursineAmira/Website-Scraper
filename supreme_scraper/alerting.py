"""
alerting.py — Stock status change notifications via Telegram or mock log.

SECURITY:
- The Telegram bot token is read from settings (environment variable only)
  and never passed as a function argument, preventing accidental log exposure.
- sensitive_filter in logging_config.py provides a secondary defense for
  any token-shaped string that might inadvertently reach a log call.
- All exceptions from the Telegram send are caught — alerting failure never
  propagates to the scheduler, so the audit trail continues uninterrupted.
- Alert message content is formatted only from sanitized ORM values —
  no raw HTML or untrusted external content is interpolated.
"""

from __future__ import annotations

import asyncio

from supreme_scraper.config import settings
from supreme_scraper.logging_config import get_logger
from supreme_scraper.store import StockStatusChange

logger = get_logger(__name__)


def _format_alert(change: StockStatusChange) -> str:
    """
    Build a human-readable Telegram message from a StockStatusChange.
    Only sanitized values from the ORM-validated object are used.
    """
    if change.new_status == "in_preview":
        label = "NEW DROP LISTED"
    elif change.new_status == "removed":
        label = "REMOVED FROM PREVIEW"
    else:
        label = f"STATUS: {change.new_status.upper()}"

    return (
        f"[Supreme Drop Alert]\n"
        f"{label}\n\n"
        f"Product: {change.product_name}\n"
        f"Was: {change.old_status}  →  Now: {change.new_status}\n"
        f"URL: {change.product_url}"
    )


class AlertService:
    """
    Sends Telegram messages for stock status transitions.
    Falls back to structured log output when credentials are absent.
    """

    def __init__(self) -> None:
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._chat_id = settings.TELEGRAM_CHAT_ID
        self._enabled = bool(self._token and self._chat_id)

        if not self._enabled:
            logger.info(
                "alerting.mock_mode",
                reason="TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not configured",
            )

    async def send_change_alert(self, change: StockStatusChange) -> None:
        """Send a single alert. Never raises — alerting failure is non-fatal."""
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
            from telegram import Bot

            async with Bot(token=self._token) as bot:
                await bot.send_message(chat_id=self._chat_id, text=message)
            logger.info("alerting.sent", product=change.product_name)

        except Exception as exc:
            # SECURITY: do not log self._token even in the error string.
            # sensitive_filter provides a secondary defense, but we avoid
            # constructing a string that contains it in the first place.
            logger.error(
                "alerting.send_failed",
                product=change.product_name,
                error=str(exc),
            )

    async def send_bulk_alerts(self, changes: list[StockStatusChange]) -> None:
        """Send one alert per change with a 1-second delay (Telegram rate limit)."""
        for i, change in enumerate(changes):
            await self.send_change_alert(change)
            if i < len(changes) - 1:
                await asyncio.sleep(1.0)
