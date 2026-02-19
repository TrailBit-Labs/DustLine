"""Integration tests against known Bitcoin transactions.

These tests hit real APIs and are skipped by default.
Run with: pytest --run-integration
"""

import pytest

import httpx

from core.graph import async_bfs


@pytest.mark.integration
@pytest.mark.asyncio
async def test_genesis_address_resolves():
    """Satoshi's genesis coinbase address resolves to a valid graph."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "DustLine/1.0 (test suite)"},
    ) as client:
        graph = await async_bfs(
            client,
            "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",
            max_depth=1,
            node_limit=10,
        )
        assert graph.root_txid != ""
        assert len(graph.nodes) >= 1
        assert len(graph.addresses_seen) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_known_txid_resolves():
    """A known transaction ID resolves and parses correctly."""
    # Pizza transaction (first known BTC purchase)
    pizza_txid = "a1075db55d416d3ca199f55b6084e2115b9345e16c5cf302fc80e9d5fbf5d48d"
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "DustLine/1.0 (test suite)"},
    ) as client:
        graph = await async_bfs(
            client,
            pizza_txid,
            max_depth=1,
            node_limit=5,
        )
        assert pizza_txid in graph.nodes
        assert graph.nodes[pizza_txid].resolved is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_invalid_target_returns_empty():
    """Invalid target returns graph with warning, no crash."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0),
        headers={"User-Agent": "DustLine/1.0 (test suite)"},
    ) as client:
        graph = await async_bfs(
            client,
            "not_a_valid_address_or_txid",
            max_depth=1,
            node_limit=5,
        )
        assert graph.root_txid == ""
        assert len(graph.warnings) > 0
