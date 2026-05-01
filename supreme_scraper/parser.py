"""
parser.py — Extracts product data from Supreme's Next.js RSC page response.

PARSING TECHNIQUE (verified against live site, 2026-05-01):
  Supreme's preview page uses Next.js App Router with React Server Components.
  All 323 products are embedded as JSON inside <script> tags in the raw HTML
  response — no JavaScript execution or browser automation is required.
  httpx fetches it identically to `curl`.

  Extraction stages:
    1. BeautifulSoup finds <script> tags (CSS selector).
    2. Regex identifies Next.js RSC push calls:
         self.__next_f.push([1, "..."])
       The string argument is a JSON-encoded (double-escaped) RSC chunk.
    3. Each chunk is decoded with json.loads('"' + chunk + '"') and
       concatenated into the full RSC payload string.
    4. The `"products":` array is located in the payload via regex,
       its bracket depth is counted to find the array boundary, and the
       result is parsed with json.loads().

SECURITY:
  - All extracted string values pass through _sanitize_text() before storage.
  - URLs are normalized and unconditionally upgraded to HTTPS.
  - stock_status is constrained to ALLOWED_STOCK_STATUSES (whitelist).
  - Per-product try/except isolation: one malformed record cannot abort
    the entire batch.
  - available_sizes always serializes to a JSON list, never raw HTML.
  - Records missing product_url or product_name are silently discarded
    (cannot be upserted without a unique key).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from supreme_scraper.logging_config import get_logger

logger = get_logger(__name__)

# ------------------------------------------------------------------ #
# Stage 1 & 2 — script tag identification                             #
# ------------------------------------------------------------------ #

SCRIPT_CSS_SELECTOR = "script"

# Matches the RSC push calls: self.__next_f.push([1,"<payload>"])
# The payload is captured as group 1.
RSC_PUSH_PATTERN: re.Pattern[str] = re.compile(
    r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
    re.DOTALL,
)

# ------------------------------------------------------------------ #
# Stage 3 — products array extraction                                  #
# ------------------------------------------------------------------ #

# Locates the start of the products JSON array within the RSC payload.
PRODUCTS_KEY_PATTERN: re.Pattern[str] = re.compile(r'"products":\s*(\[)')

# ------------------------------------------------------------------ #
# Product URL construction                                             #
# ------------------------------------------------------------------ #

BASE_URL = "https://supreme.com"
PREVIEW_SLUG = "springsummer2026"
PRODUCT_URL_TEMPLATE = f"{BASE_URL}/previews/{PREVIEW_SLUG}/{{slug}}"

# ------------------------------------------------------------------ #
# Validation / data-minimization constants                             #
# ------------------------------------------------------------------ #

MAX_PRODUCT_NAME_LEN = 256
MAX_SKU_LEN = 128
MAX_URL_LEN = 1024
MAX_CATEGORY_LEN = 128
MAX_NOTES_LEN = 512

ALLOWED_STOCK_STATUSES: frozenset[str] = frozenset(
    {"in_preview", "removed", "unknown"}
)

# Excludes \x09 (tab), \x0a (LF), \x0d (CR) so they are collapsed by the
# subsequent \s+ substitution rather than silently deleted.
_CONTROL_CHARS: re.Pattern[str] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


# ------------------------------------------------------------------ #
# Sanitization helpers                                                 #
# ------------------------------------------------------------------ #


def _sanitize_text(value: Any, max_len: int = 256) -> str | None:
    """
    Strip, collapse whitespace, remove control characters, and truncate.
    Returns None if the cleaned value is empty.
    """
    if not isinstance(value, str) or not value:
        return None
    cleaned = _CONTROL_CHARS.sub("", value.strip())
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:max_len] if cleaned else None


def _normalize_url(href: Any, base: str = BASE_URL) -> str | None:
    """
    Resolve relative URLs, enforce HTTPS scheme, validate structure.
    Returns None for invalid, empty, or off-scheme URLs.
    """
    if not isinstance(href, str) or not href:
        return None
    url = urljoin(base, href.strip())
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http"):
        return None
    if not parsed.netloc:
        return None
    if parsed.scheme == "http":
        url = "https://" + url[len("http://"):]
    return url[:MAX_URL_LEN]


# ------------------------------------------------------------------ #
# RSC payload extraction (stages 1–3)                                  #
# ------------------------------------------------------------------ #


def _extract_products_json(html: str) -> list[dict[str, Any]]:
    """
    Parse raw HTML and extract the products JSON array from the
    Next.js RSC (React Server Components) payload.

    Returns an empty list if the HTML is malformed, the payload is absent,
    or the products key is not found. All errors are logged.
    """
    if not html:
        logger.warning("parser.empty_html")
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:
        logger.error("parser.html_parse_failed", error=str(exc))
        return []

    # Stage 1: collect all <script> tags (CSS selector)
    script_tags = soup.select(SCRIPT_CSS_SELECTOR)

    # Stage 2: identify RSC push calls and decode their payloads
    full_payload = ""
    chunk_count = 0
    for tag in script_tags:
        raw = tag.string or ""
        for match in RSC_PUSH_PATTERN.finditer(raw):
            chunk = match.group(1)
            try:
                # Each chunk is a JSON-encoded string (double-escaped).
                decoded: str = json.loads('"' + chunk + '"')
                full_payload += decoded
                chunk_count += 1
            except (json.JSONDecodeError, ValueError):
                # Skip malformed chunks — one bad chunk does not abort parsing.
                logger.debug("parser.chunk_decode_failed", chunk_preview=chunk[:80])

    if not full_payload:
        logger.warning(
            "parser.no_rsc_payload",
            script_tags_found=len(script_tags),
            hint="page structure may have changed",
        )
        return []

    logger.debug("parser.rsc_chunks_decoded", count=chunk_count)

    # Stage 3: find and extract the products array
    m = PRODUCTS_KEY_PATTERN.search(full_payload)
    if not m:
        logger.warning(
            "parser.products_key_not_found",
            payload_length=len(full_payload),
            hint="RSC chunk key for products may have changed",
        )
        return []

    # bracket-count to find the end of the array
    start = m.end() - 1  # position of the opening '['
    depth = 0
    end = start
    for i, ch in enumerate(full_payload[start:]):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if depth == 0 and i > 0:
            end = start + i + 1
            break

    products_json = full_payload[start:end]
    try:
        products: list[dict[str, Any]] = json.loads(products_json)
    except json.JSONDecodeError as exc:
        logger.error("parser.products_json_invalid", error=str(exc))
        return []

    if not isinstance(products, list):
        logger.error("parser.products_not_a_list", type=type(products).__name__)
        return []

    logger.info("parser.products_extracted", count=len(products))
    return products


# ------------------------------------------------------------------ #
# Per-product mapping                                                  #
# ------------------------------------------------------------------ #


def _map_product(raw: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """
    Map one raw product dict (from the RSC JSON) to a Drop schema dict.
    Returns None if required fields (product_url, product_name) are missing.
    """
    slug = _sanitize_text(raw.get("slug"), max_len=200)
    if not slug:
        return None

    product_url = _normalize_url(PRODUCT_URL_TEMPLATE.format(slug=slug))
    if not product_url:
        return None

    product_name = _sanitize_text(raw.get("title"), max_len=MAX_PRODUCT_NAME_LEN)
    if not product_name:
        return None

    sku = _sanitize_text(raw.get("_id"), max_len=MAX_SKU_LEN)

    # category.title repurposed for the colorway field
    category = raw.get("category") or {}
    colorway = _sanitize_text(category.get("title"), max_len=MAX_CATEGORY_LEN)

    # season.title used as human-readable drop_date
    season = raw.get("season") or {}
    drop_date = _sanitize_text(season.get("title"), max_len=64)

    # First variant title stored in notes for context
    variants: list[dict[str, Any]] = raw.get("variants") or []
    first_variant_title = None
    image_asset_id = None
    if variants:
        v0 = variants[0]
        first_variant_title = _sanitize_text(v0.get("title"), max_len=128)
        images: list[dict[str, Any]] = v0.get("images") or []
        if images:
            asset = images[0].get("asset") or {}
            # Store the Sanity assetId as a reference; full CDN URL
            # construction requires the Sanity project ID (see README).
            image_asset_id = _sanitize_text(asset.get("assetId"), max_len=256)

    notes = first_variant_title

    stock_status = "in_preview"  # all listed products are in the preview

    return {
        "product_name": product_name,
        "brand": "Supreme",
        "sku": sku,
        "colorway": colorway,
        "retail_price": None,   # preview page has no pricing data
        "currency": "USD",
        "drop_date": drop_date,
        "drop_method": "online",
        "stock_status": stock_status,
        "available_sizes": json.dumps([]),  # not listed on the preview page
        "product_url": product_url,
        "image_url": image_asset_id,  # Sanity assetId used as image reference
        "source_website": "supreme.com",
        "scrape_timestamp": now,
        "resale_low": None,
        "resale_high": None,
        "notes": notes,
    }


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #


def parse_preview_page(html: str, source_url: str) -> list[dict[str, Any]]:
    """
    Parse the raw HTML of supreme.com/previews/springsummer2026/all
    into a list of validated, sanitized product dicts matching the Drop schema.

    Args:
        html:       Raw HTML string returned by Crawler.fetch().
        source_url: The canonical URL that was fetched (used for logging).

    Returns:
        List of dicts. Empty list if no products found or HTML is malformed.
    """
    now = datetime.now(timezone.utc)

    raw_products = _extract_products_json(html)
    if not raw_products:
        return []

    results: list[dict[str, Any]] = []
    skipped = 0

    for raw in raw_products:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        try:
            record = _map_product(raw, now)
        except Exception as exc:
            logger.warning("parser.product_map_error", error=str(exc))
            skipped += 1
            continue

        if record is None:
            skipped += 1
            continue

        # Final whitelist check on stock_status
        if record["stock_status"] not in ALLOWED_STOCK_STATUSES:
            record["stock_status"] = "unknown"

        results.append(record)

    logger.info(
        "parser.done",
        url=source_url,
        parsed=len(results),
        skipped=skipped,
    )
    return results
