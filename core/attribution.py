"""Multi-source entity attribution for Bitcoin addresses.

Three-tier attribution pipeline:
  Tier 1: Local SQLite database (instant, offline)
  Tier 2: WalletExplorer cluster lookup (0.8 req/s, rate-limited)
  Tier 3: Arkham Intelligence API (optional, ~5 req/s, requires API key)

Attribution runs as a batch pass AFTER BFS traversal to avoid
rate-limited APIs blocking graph construction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

import httpx

from core import AttributionResult, AttributionSummary, GraphResult
from core.rate_limiter import ARKHAM_LIMITER, WALLETEXPLORER_LIMITER

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
KNOWN_ENTITIES_DB = DATA_DIR / "known_entities.db"
KNOWN_ENTITIES_JSON = DATA_DIR / "known_entities.json"

WALLETEXPLORER_URL = "https://www.walletexplorer.com/api/1/address"
ARKHAM_API_URL = "https://api.arkhamintelligence.com/intelligence/address"

# Maximum addresses to query via WalletExplorer (at 0.8 req/s, 200 = ~4 min)
DEFAULT_WE_LIMIT = 200


class EntityDatabase:
    """SQLite-backed entity lookup with JSON fallback.

    Attempts to load from known_entities.db first. If the SQLite file
    does not exist, falls back to known_entities.json for backward
    compatibility.
    """

    def __init__(self):
        self._conn: Optional[sqlite3.Connection] = None
        self._fallback: dict[str, tuple[str, str]] = {}  # addr -> (entity, category)
        self._loaded = False
        self._using_sqlite = False

    def load(self, db_path: Path = KNOWN_ENTITIES_DB) -> None:
        """Load the entity database (SQLite or JSON fallback)."""
        if self._loaded:
            return

        if db_path.exists():
            self._conn = sqlite3.connect(str(db_path))
            self._conn.row_factory = sqlite3.Row
            self._using_sqlite = True
            logger.info("Loaded entity database: %s", db_path)
        else:
            self._load_json_fallback()
            logger.info(
                "SQLite DB not found, fell back to JSON (%d entries)",
                len(self._fallback),
            )

        self._loaded = True

    def _load_json_fallback(self) -> None:
        """Load known_entities.json as fallback when .db is missing."""
        if not KNOWN_ENTITIES_JSON.exists():
            logger.warning("Known entities JSON not found: %s", KNOWN_ENTITIES_JSON)
            return

        category_map = {
            "exchanges": "exchange",
            "mining_pools": "mining_pool",
            "services": "service",
            "notable": "notable",
        }

        with open(KNOWN_ENTITIES_JSON, "r") as f:
            data = json.load(f)

        for cat_key, cat_entries in data.get("entities", {}).items():
            category = category_map.get(cat_key, cat_key)
            for entity_data in cat_entries.values():
                name = entity_data.get("name", "Unknown")
                for addr in entity_data.get("known_addresses", []):
                    self._fallback[addr] = (name, category)

    def lookup(self, address: str) -> Optional[AttributionResult]:
        """Look up an address. Returns AttributionResult or None."""
        if not self._loaded:
            self.load()

        if self._using_sqlite:
            cursor = self._conn.execute(
                "SELECT entity, category, confidence FROM entities WHERE address = ?",
                (address,),
            )
            row = cursor.fetchone()
            if row:
                return AttributionResult(
                    address=address,
                    entity=row["entity"],
                    source="local_db",
                    category=row["category"] or "",
                    confidence=row["confidence"] or "confirmed",
                )
            return None
        else:
            entry = self._fallback.get(address)
            if entry:
                return AttributionResult(
                    address=address,
                    entity=entry[0],
                    source="local_db",
                    category=entry[1],
                    confidence="confirmed",
                )
            return None

    def lookup_name(self, address: str) -> Optional[str]:
        """Backward-compatible lookup: returns entity name or None."""
        result = self.lookup(address)
        return result.entity if result else None

    def close(self) -> None:
        """Close the SQLite connection if open."""
        if self._conn:
            self._conn.close()
            self._conn = None


# Module-level singleton
_entity_db = EntityDatabase()


async def attribute_graph(
    client: httpx.AsyncClient,
    graph: GraphResult,
    *,
    skip_walletexplorer: bool = False,
    we_limit: Optional[int] = DEFAULT_WE_LIMIT,
    arkham_key: Optional[str] = None,
    progress_callback=None,
) -> GraphResult:
    """Three-tier attribution pipeline. Modifies graph in-place.

    Args:
        client: Shared httpx async client.
        graph: Completed BFS graph result.
        skip_walletexplorer: If True, skip Tier 2.
        we_limit: Max addresses to query via WalletExplorer.
        arkham_key: Arkham Intelligence API key (enables Tier 3).
        progress_callback: Optional callable(attributed, total) for progress.

    Returns:
        The same GraphResult with attribution data populated.
    """
    _entity_db.load()

    # Build address -> list of node txids mapping
    address_nodes: dict[str, list[str]] = {}
    for node in graph.nodes.values():
        for inp in node.inputs:
            if inp.address:
                address_nodes.setdefault(inp.address, []).append(node.txid)
        for out in node.outputs:
            if out.address:
                address_nodes.setdefault(out.address, []).append(node.txid)

    all_addresses = list(address_nodes.keys())
    total = len(all_addresses)
    results: list[AttributionResult] = []
    resolved: dict[str, AttributionResult] = {}
    by_source: dict[str, int] = {}
    by_category: dict[str, int] = {}

    # ── Tier 1: Local SQLite database (instant, offline) ─────────────────────
    for addr in all_addresses:
        result = _entity_db.lookup(addr)
        if result:
            resolved[addr] = result
            results.append(result)
            _apply_attribution(graph, address_nodes[addr], addr, result.entity)
            by_source["local_db"] = by_source.get("local_db", 0) + 1
            if result.category:
                by_category[result.category] = (
                    by_category.get(result.category, 0) + 1
                )

    if progress_callback:
        progress_callback(len(resolved), total)

    # ── Tier 2: WalletExplorer (rate-limited, slow) ──────────────────────────
    unmatched = [a for a in all_addresses if a not in resolved]

    if not skip_walletexplorer:
        to_query = unmatched if we_limit is None else unmatched[:we_limit]

        if we_limit is not None and len(unmatched) > we_limit:
            graph.warnings.append(
                f"WalletExplorer: queried {we_limit} of {len(unmatched)} "
                f"unattributed addresses (capped for speed). "
                f"Use --thorough to check all."
            )

        for addr in to_query:
            entity = await _query_walletexplorer(client, addr)
            if entity:
                ar = AttributionResult(
                    address=addr,
                    entity=entity,
                    source="walletexplorer",
                    confidence="cluster",
                )
                resolved[addr] = ar
                results.append(ar)
                _apply_attribution(graph, address_nodes[addr], addr, entity)
                by_source["walletexplorer"] = (
                    by_source.get("walletexplorer", 0) + 1
                )
            if progress_callback:
                progress_callback(len(resolved), total)

        graph.we_addresses_queried = len(to_query)
        graph.we_addresses_total_unmatched = len(unmatched)
    else:
        graph.we_addresses_queried = 0
        graph.we_addresses_total_unmatched = len(unmatched)

    # ── Tier 3: Arkham Intelligence (optional, requires API key) ─────────────
    if arkham_key:
        still_unmatched = [a for a in all_addresses if a not in resolved]
        for addr in still_unmatched:
            ar = await _query_arkham(client, addr, arkham_key)
            if ar:
                resolved[addr] = ar
                results.append(ar)
                _apply_attribution(graph, address_nodes[addr], addr, ar.entity)
                by_source["arkham"] = by_source.get("arkham", 0) + 1
                if ar.category:
                    by_category[ar.category] = (
                        by_category.get(ar.category, 0) + 1
                    )
            if progress_callback:
                progress_callback(len(resolved), total)

    # ── Build summary ────────────────────────────────────────────────────────
    sources_used = ["local_db"]
    if not skip_walletexplorer:
        sources_used.append("walletexplorer")
    if arkham_key:
        sources_used.append("arkham")

    attributed_count = len(resolved)
    graph.attribution_results = results
    graph.attribution_summary = AttributionSummary(
        total_addresses=total,
        attributed_count=attributed_count,
        by_source=by_source,
        by_category=by_category,
        coverage_rate=attributed_count / max(total, 1),
        sources_used=sources_used,
    )

    return graph


def _apply_attribution(
    graph: GraphResult,
    txids: list[str],
    address: str,
    entity: str,
) -> None:
    """Apply an entity attribution to all nodes containing this address."""
    for txid in txids:
        if txid in graph.nodes:
            graph.nodes[txid].attributed_entities[address] = entity


async def _query_walletexplorer(
    client: httpx.AsyncClient,
    address: str,
) -> Optional[str]:
    """Query WalletExplorer for an address label. Never raises."""
    async with WALLETEXPLORER_LIMITER:
        try:
            resp = await client.get(
                WALLETEXPLORER_URL,
                params={"address": address, "caller": "dustline"},
                timeout=15.0,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("found") or data.get("_found"):
                return data.get("label") or data.get("wallet_name")
            return None
        except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            logger.debug("WalletExplorer failed for %s: %s", address, exc)
            return None
        except Exception as exc:
            logger.debug("WalletExplorer unexpected error for %s: %s", address, exc)
            return None


async def _query_arkham(
    client: httpx.AsyncClient,
    address: str,
    api_key: str,
) -> Optional[AttributionResult]:
    """Query Arkham Intelligence for an address label. Never raises."""
    async with ARKHAM_LIMITER:
        try:
            resp = await client.get(
                f"{ARKHAM_API_URL}/{address}",
                headers={"API-Key": api_key},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            # Arkham returns entity info under "arkhamEntity" or "arkhamLabel"
            entity_data = data.get("arkhamEntity") or {}
            entity_name = entity_data.get("name", "")
            if not entity_name:
                entity_name = data.get("arkhamLabel", {}).get("name", "")
            if not entity_name:
                return None

            category = entity_data.get("type", "").lower()

            return AttributionResult(
                address=address,
                entity=entity_name,
                source="arkham",
                category=category,
                confidence="probable",
            )
        except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            logger.debug("Arkham failed for %s: %s", address, exc)
            return None
        except Exception as exc:
            logger.debug("Arkham unexpected error for %s: %s", address, exc)
            return None
