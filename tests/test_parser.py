"""
test_parser.py — Unit tests for parser.py.

All tests are offline (no network calls). The fixture HTML at
tests/fixtures/supreme_preview_all.html mimics the real Next.js RSC payload.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from supreme_scraper.parser import (
    BASE_URL,
    ALLOWED_STOCK_STATUSES,
    _extract_products_json,
    _normalize_url,
    _sanitize_text,
    parse_preview_page,
)

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "supreme_preview_all.html"
FIXTURE_HTML = FIXTURE_PATH.read_text(encoding="utf-8")
SOURCE_URL = "https://supreme.com/previews/springsummer2026/all"


# ------------------------------------------------------------------ #
# _sanitize_text                                                       #
# ------------------------------------------------------------------ #


class TestSanitizeText:
    def test_strips_leading_trailing_whitespace(self):
        assert _sanitize_text("  hello world  ") == "hello world"

    def test_collapses_internal_whitespace(self):
        assert _sanitize_text("foo   bar\ttab") == "foo bar tab"

    def test_removes_control_characters(self):
        assert _sanitize_text("name\x00\x1f\x7f") == "name"

    def test_enforces_max_length(self):
        result = _sanitize_text("a" * 300, max_len=256)
        assert result is not None
        assert len(result) == 256

    def test_returns_none_for_empty_string(self):
        assert _sanitize_text("") is None

    def test_returns_none_for_none(self):
        assert _sanitize_text(None) is None

    def test_returns_none_for_non_string(self):
        assert _sanitize_text(42) is None  # type: ignore[arg-type]

    def test_returns_none_for_whitespace_only(self):
        assert _sanitize_text("   ") is None


# ------------------------------------------------------------------ #
# _normalize_url                                                       #
# ------------------------------------------------------------------ #


class TestNormalizeUrl:
    def test_resolves_relative_url(self):
        result = _normalize_url("/previews/springsummer2026/test-jacket", BASE_URL)
        assert result == "https://supreme.com/previews/springsummer2026/test-jacket"

    def test_passes_through_absolute_https(self):
        url = "https://supreme.com/previews/springsummer2026/jacket"
        assert _normalize_url(url) == url

    def test_upgrades_http_to_https(self):
        result = _normalize_url("http://supreme.com/shop/abc", BASE_URL)
        assert result is not None
        assert result.startswith("https://")

    def test_returns_none_for_empty(self):
        assert _normalize_url("") is None
        assert _normalize_url(None) is None

    def test_returns_none_for_javascript_scheme(self):
        assert _normalize_url("javascript:alert(1)") is None

    def test_returns_none_for_data_scheme(self):
        assert _normalize_url("data:text/html,<h1>x</h1>") is None

    def test_truncates_long_url(self):
        long_path = "/" + "a" * 2000
        result = _normalize_url(long_path, BASE_URL)
        assert result is not None
        assert len(result) <= 1024


# ------------------------------------------------------------------ #
# _extract_products_json                                               #
# ------------------------------------------------------------------ #


class TestExtractProductsJson:
    def test_extracts_correct_count_from_fixture(self):
        products = _extract_products_json(FIXTURE_HTML)
        # Fixture has 4 raw entries including the one with empty slug
        assert len(products) == 4

    def test_returns_list_of_dicts(self):
        products = _extract_products_json(FIXTURE_HTML)
        assert all(isinstance(p, dict) for p in products)

    def test_empty_html_returns_empty_list(self):
        assert _extract_products_json("") == []

    def test_garbage_html_returns_empty_list(self):
        assert _extract_products_json("<html><body>no rsc here</body></html>") == []

    def test_html_without_products_key_returns_empty_list(self):
        html = (
            '<script>self.__next_f.push([1,"some payload without products key"])'
            "</script>"
        )
        assert _extract_products_json(html) == []

    def test_first_product_has_expected_keys(self):
        products = _extract_products_json(FIXTURE_HTML)
        p = products[0]
        assert "title" in p
        assert "slug" in p
        assert "_id" in p
        assert "category" in p
        assert "variants" in p


# ------------------------------------------------------------------ #
# parse_preview_page                                                   #
# ------------------------------------------------------------------ #


class TestParsePreviewPage:
    def setup_method(self):
        self.records = parse_preview_page(FIXTURE_HTML, SOURCE_URL)

    def test_returns_three_valid_records(self):
        # 4 raw products in fixture, but 1 has empty slug — discarded
        assert len(self.records) == 3

    def test_product_urls_are_https(self):
        for r in self.records:
            assert r["product_url"].startswith("https://supreme.com/previews/")

    def test_product_names_are_sanitized(self):
        for r in self.records:
            assert r["product_name"] is not None
            assert "\x00" not in r["product_name"]
            assert len(r["product_name"]) <= 256

    def test_sku_is_sanity_id(self):
        jacket = next(r for r in self.records if "Jacket" in r["product_name"])
        assert jacket["sku"] == "aaaaaaaa-0000-0000-0000-000000000001"

    def test_stock_status_is_in_preview(self):
        for r in self.records:
            assert r["stock_status"] == "in_preview"

    def test_stock_status_in_allowed_set(self):
        for r in self.records:
            assert r["stock_status"] in ALLOWED_STOCK_STATUSES

    def test_available_sizes_is_valid_json_list(self):
        for r in self.records:
            parsed = json.loads(r["available_sizes"])
            assert isinstance(parsed, list)

    def test_brand_is_supreme(self):
        for r in self.records:
            assert r["brand"] == "Supreme"

    def test_product_with_variants_has_image_url(self):
        jacket = next(r for r in self.records if "Jacket" in r["product_name"])
        assert jacket["image_url"] == "4a5adb23d52db7849a3eccb81d6dbeab5e986b31"

    def test_product_without_variants_has_null_image_url(self):
        bag = next(r for r in self.records if "Bag" in r["product_name"])
        assert bag["image_url"] is None

    def test_colorway_from_category(self):
        jacket = next(r for r in self.records if "Jacket" in r["product_name"])
        assert jacket["colorway"] == "Jackets"

    def test_empty_html_returns_empty_list(self):
        assert parse_preview_page("", SOURCE_URL) == []

    def test_garbage_html_returns_empty_list(self):
        assert parse_preview_page("<<< not html >>>", SOURCE_URL) == []

    def test_internal_whitespace_collapsed_in_name(self):
        # Fixture product 3 has double space in title: "Test Box Logo Tee  Special"
        tee = next(r for r in self.records if "Tee" in r["product_name"])
        assert "  " not in tee["product_name"]  # double space collapsed
