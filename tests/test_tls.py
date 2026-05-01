"""
test_tls.py — TLS enforcement assertions.

These tests verify the structural guarantee that TLS certificate validation
cannot be disabled through configuration. They do not make network calls.
"""

from __future__ import annotations

import inspect

import certifi
import pytest
from pathlib import Path

from supreme_scraper.crawler import Crawler
from supreme_scraper.config import Settings


class TestTLSEnforcement:
    def test_settings_has_no_tls_toggle_field(self):
        """
        Settings must not expose any field that could disable TLS.
        The absence of these fields is the security control.
        """
        field_names = set(Settings.__dataclass_fields__.keys())
        forbidden = {"tls_verify", "verify", "ssl_verify", "disable_ssl", "insecure"}
        overlap = field_names & forbidden
        assert not overlap, (
            f"Settings exposes TLS toggle field(s): {overlap}. "
            "These must not exist — TLS enforcement is unconditional."
        )

    def test_crawler_aenter_uses_certifi(self):
        """
        Crawler.__aenter__ source must reference certifi.where() as the
        verify argument — never False or a config variable.
        """
        source = inspect.getsource(Crawler.__aenter__)
        assert "certifi.where()" in source, (
            "Crawler.__aenter__ must use certifi.where() as the verify argument."
        )

    def test_crawler_aenter_never_disables_verify(self):
        """verify=False must not appear anywhere in Crawler.__aenter__."""
        source = inspect.getsource(Crawler.__aenter__)
        assert "verify=False" not in source, (
            "verify=False must never appear in Crawler.__aenter__."
        )

    def test_certifi_ca_bundle_exists_on_disk(self):
        """The CA bundle file that certifi points to must exist."""
        bundle_path = Path(certifi.where())
        assert bundle_path.exists(), (
            f"certifi CA bundle not found at {bundle_path}. "
            "Run: pip install certifi"
        )

    def test_certifi_ca_bundle_is_a_file(self):
        """The CA bundle must be a regular file, not a directory."""
        assert Path(certifi.where()).is_file()

    def test_config_ca_bundle_matches_certifi(self):
        """Settings.CA_BUNDLE must point to the same file as certifi.where()."""
        from supreme_scraper.config import settings
        assert settings.CA_BUNDLE == certifi.where()
