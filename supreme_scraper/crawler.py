"""
crawler.py — Async HTTP client with TLS enforcement, robots.txt compliance,
and polite rate limiting.

SECURITY:
- verify=certifi.where() is HARDCODED. It is not a variable, not in config,
  and cannot be disabled at runtime. certifi is listed explicitly in
  requirements.txt so CA bundle updates are tracked in version control.
- robots.txt is fetched and parsed before the first crawl request. If the
  target URL is disallowed, CrawlDisallowedError is raised and the scrape
  cycle is aborted (recorded in ScrapeLog but no HTTP request is made).
- Rate limiting enforces a minimum 2-second gap between requests using
  time.monotonic() (immune to wall-clock adjustments).
- max_redirects=3 limits open-redirect chains.
- The User-Agent identifies the bot non-deceptively.
"""

from __future__ import annotations

import asyncio
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional

import certifi
import httpx

from supreme_scraper.config import settings
from supreme_scraper.logging_config import get_logger

logger = get_logger(__name__)

_MIN_REQUEST_INTERVAL_SECONDS = 2.0


class CrawlDisallowedError(Exception):
    """Raised when robots.txt forbids crawling the target URL."""


@dataclass(slots=True)
class FetchResult:
    html: str
    status_code: int
    duration_ms: int
    url: str


class RobotsTxtGate:
    """
    Fetches and caches robots.txt once per Crawler lifetime.

    Uses httpx (not urllib.request) so the robots.txt fetch uses the same
    User-Agent and TLS settings as the actual scrape requests. This matters
    because some servers (including supreme.com) return 403 to Python's
    default urllib UA while serving 200 to a browser-like UA.

    RFC 9309 §2.3.1 status handling:
      200      → parse normally
      401/403  → disallow all (server explicitly forbids bots)
      404/410  → allow all (no rules defined)
      5xx      → allow all (temporary unavailability)
      network  → allow all, log warning
    """

    def __init__(self, robots_url: str, user_agent: str) -> None:
        self._robots_url = robots_url
        self._user_agent = user_agent
        self._parser = urllib.robotparser.RobotFileParser()
        self._loaded = False

    def load(self) -> None:
        """
        Synchronous fetch of robots.txt using httpx with our configured
        User-Agent. Called once inside Crawler.__aenter__ before any requests.
        """
        self._parser.set_url(self._robots_url)
        try:
            response = httpx.get(
                self._robots_url,
                headers={"User-Agent": self._user_agent},
                verify=certifi.where(),
                timeout=10.0,
                follow_redirects=True,
            )
            if response.status_code == 200:
                self._parser.parse(response.text.splitlines())
                logger.info(
                    "robots.loaded",
                    url=self._robots_url,
                    status=response.status_code,
                )
            elif response.status_code in (401, 403):
                # Server explicitly forbids bot access — disallow all (RFC 9309)
                logger.warning(
                    "robots.access_denied",
                    url=self._robots_url,
                    status=response.status_code,
                    decision="disallow_all (RFC 9309 §2.3.1)",
                )
                # Parser with no entries defaults to disallow-all
            else:
                # 404, 410, 5xx — treat as allow-all (RFC 9309)
                self._parser.parse(["User-agent: *", "Allow: /"])
                logger.info(
                    "robots.not_found_allow_all",
                    url=self._robots_url,
                    status=response.status_code,
                )
        except Exception as exc:
            # Network failure — fail open, log for audit (RFC 9309 §2.3.1)
            self._parser.parse(["User-agent: *", "Allow: /"])
            logger.warning(
                "robots.fetch_failed",
                url=self._robots_url,
                error=str(exc),
                decision="allow_all (RFC 9309 §2.3.1)",
            )
        finally:
            self._loaded = True

    def is_allowed(self, url: str) -> bool:
        if not self._loaded:
            raise RuntimeError(
                "RobotsTxtGate.load() must be called before is_allowed()"
            )
        return self._parser.can_fetch(self._user_agent, url)


class Crawler:
    """
    Async context manager wrapping httpx.AsyncClient.

    Usage:
        async with Crawler() as crawler:
            result = await crawler.fetch(url)
    """

    def __init__(self) -> None:
        self._robots_gate = RobotsTxtGate(settings.ROBOTS_URL, settings.USER_AGENT)
        self._last_request_time: float = 0.0
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "Crawler":
        # SECURITY: verify=certifi.where() — explicit CA bundle, hardcoded.
        # Never True (which uses the OS bundle) and never False.
        self._client = httpx.AsyncClient(
            verify=certifi.where(),
            headers={
                "User-Agent": settings.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
            timeout=httpx.Timeout(settings.REQUEST_TIMEOUT),
            follow_redirects=True,
            max_redirects=3,
        )
        self._robots_gate.load()
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_val: object,
        exc_tb: object,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def _enforce_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_time
        wait = _MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if wait > 0:
            logger.debug("crawler.rate_limit.waiting", wait_seconds=round(wait, 2))
            await asyncio.sleep(wait)

    async def fetch(self, url: str) -> FetchResult:
        """
        Fetch a URL after robots.txt and rate-limit checks.

        Returns FetchResult on HTTP 200–299.
        Raises CrawlDisallowedError if robots.txt disallows the URL.
        Raises httpx.HTTPStatusError on 4xx/5xx responses.
        """
        if not self._robots_gate.is_allowed(url):
            logger.warning("crawler.disallowed_by_robots", url=url)
            raise CrawlDisallowedError(f"robots.txt disallows: {url}")

        await self._enforce_rate_limit()

        t_start = time.monotonic()
        logger.info("crawler.fetching", url=url)

        assert self._client is not None, "Crawler must be used as a context manager"
        response = await self._client.get(url)
        duration_ms = int((time.monotonic() - t_start) * 1000)
        self._last_request_time = time.monotonic()

        logger.info(
            "crawler.response",
            url=url,
            status_code=response.status_code,
            duration_ms=duration_ms,
            final_url=str(response.url),
        )

        response.raise_for_status()

        return FetchResult(
            html=response.text,
            status_code=response.status_code,
            duration_ms=duration_ms,
            url=str(response.url),
        )
