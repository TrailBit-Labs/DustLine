"""Tests for entity attribution."""

import pytest
from pathlib import Path

from core import AttributionResult
from core.attribution import EntityDatabase


# ── Local entity database tests (SQLite path) ────────────────────────────────


def test_local_lookup_known_address():
    """Known genesis address returns entity name via SQLite."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert result is not None
    assert result.entity == "Satoshi Nakamoto (Genesis)"
    assert result.source == "local_db"


def test_local_lookup_binance():
    """Known Binance address returns entity with category."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s")
    assert result is not None
    assert result.entity == "Binance"
    assert result.category == "exchange"


def test_local_lookup_unknown_address():
    """Unknown address returns None."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1UnknownAddressThatDoesNotExistXYZ")
    assert result is None


def test_local_lookup_coinbase():
    """Known Coinbase address returns entity."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1461dNnoDodFqmjMBBFiFJSzuBbPkt2biU")
    assert result is not None
    assert result.entity == "Coinbase"


# ── AttributionResult type tests ─────────────────────────────────────────────


def test_lookup_returns_attribution_result():
    """Lookup returns an AttributionResult, not a plain string."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert isinstance(result, AttributionResult)
    assert result.address == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"


def test_local_db_category_populated():
    """Category field is populated for known addresses."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s")
    assert result is not None
    assert result.category in ("exchange", "mining_pool", "service", "notable")


def test_local_db_confidence_populated():
    """Confidence field defaults to 'confirmed' for manual entries."""
    db = EntityDatabase()
    db.load()
    result = db.lookup("1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s")
    assert result is not None
    assert result.confidence == "confirmed"


# ── JSON fallback tests ──────────────────────────────────────────────────────


def test_json_fallback_when_no_sqlite():
    """Falls back to JSON when SQLite DB does not exist."""
    db = EntityDatabase()
    db.load(db_path=Path("/nonexistent/path/to/db.db"))
    result = db.lookup("1NDyJtNTjmwk5xPNhjgAMu4HDHigtobu1s")
    assert result is not None
    assert result.entity == "Binance"
    assert result.source == "local_db"
    assert result.category == "exchange"


def test_json_fallback_unknown_returns_none():
    """JSON fallback also returns None for unknown addresses."""
    db = EntityDatabase()
    db.load(db_path=Path("/nonexistent/path/to/db.db"))
    result = db.lookup("1UnknownAddressThatDoesNotExistXYZ")
    assert result is None


# ── Backward-compatible lookup_name ──────────────────────────────────────────


def test_lookup_name_returns_string():
    """lookup_name returns entity name as string for backward compat."""
    db = EntityDatabase()
    db.load()
    name = db.lookup_name("1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa")
    assert name == "Satoshi Nakamoto (Genesis)"


def test_lookup_name_returns_none_for_unknown():
    """lookup_name returns None for unknown addresses."""
    db = EntityDatabase()
    db.load()
    name = db.lookup_name("1UnknownAddressThatDoesNotExistXYZ")
    assert name is None
