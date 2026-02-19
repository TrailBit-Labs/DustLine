"""Async BFS graph traversal for Bitcoin transactions.

Traverses the transaction graph starting from an address or txid,
using an asyncio worker pool with rate-limited API calls.
Primary provider: mempool.space. Fallback: Blockstream.info.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from core import (
    GraphEdge,
    GraphNode,
    GraphResult,
    ScriptType,
    TxInput,
    TxOutput,
)
from core.rate_limiter import BLOCKSTREAM_LIMITER, MEMPOOL_LIMITER

logger = logging.getLogger(__name__)

MEMPOOL_BASE = "https://mempool.space/api"
BLOCKSTREAM_BASE = "https://blockstream.info/api"

# Regex patterns for input type detection
TXID_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
# Bitcoin addresses: legacy (1), P2SH (3), bech32 (bc1)
ADDRESS_PATTERN = re.compile(r"^(1|3|bc1)[a-zA-Z0-9]{25,62}$")

NUM_WORKERS = 5
WORKER_TIMEOUT = 3.0  # Seconds to wait for new work before checking exit


async def async_bfs(
    client: httpx.AsyncClient,
    target: str,
    max_depth: int = 5,
    node_limit: int = 500,
    direction: str = "forward",
    progress_callback=None,
) -> GraphResult:
    """Traverse the Bitcoin transaction graph via async BFS.

    Args:
        client: Shared httpx async client.
        target: Bitcoin address or transaction ID.
        max_depth: Maximum BFS hops from root.
        node_limit: Maximum transaction nodes to visit.
        direction: "forward" (follow outputs), "backward" (follow inputs),
                   or "both".
        progress_callback: Optional callable(visited, node_limit, depth) for
                          progress reporting.

    Returns:
        GraphResult with all traversed nodes, edges, and metadata.
    """
    # Resolve input to a root txid
    root_txid = await _resolve_target(client, target)
    if root_txid is None:
        result = GraphResult(root_input=target, root_txid="")
        result.warnings.append(f"Could not resolve target: {target}")
        return result

    result = GraphResult(root_input=target, root_txid=root_txid)
    result.requested_max_depth = max_depth
    result_lock = asyncio.Lock()

    visited: set[str] = {root_txid}
    frontier: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
    await frontier.put((root_txid, 0))

    active_workers = 0
    active_lock = asyncio.Lock()

    async def worker():
        nonlocal active_workers

        while True:
            try:
                txid, depth = await asyncio.wait_for(
                    frontier.get(), timeout=WORKER_TIMEOUT
                )
            except asyncio.TimeoutError:
                async with active_lock:
                    if active_workers == 0 and frontier.empty():
                        return
                continue

            async with active_lock:
                active_workers += 1

            try:
                # Fetch transaction data
                tx_data = await _fetch_tx_with_fallback(client, txid)

                if tx_data is None:
                    node = GraphNode(txid=txid, depth=depth, resolved=False)
                    async with result_lock:
                        result.nodes[txid] = node
                        result.unresolved_count += 1
                    frontier.task_done()
                    continue

                # Fetch outspend data (needed for forward traversal)
                outspends = None
                if direction in ("forward", "both"):
                    outspends = await _fetch_outspends_with_fallback(client, txid)

                # Parse into GraphNode
                node = _parse_tx(tx_data, depth, outspends)

                async with result_lock:
                    result.nodes[txid] = node

                    # Collect addresses
                    for inp in node.inputs:
                        if inp.address:
                            result.addresses_seen.add(inp.address)
                    for out in node.outputs:
                        if out.address:
                            result.addresses_seen.add(out.address)

                    # Update max depth
                    if depth > result.max_depth_reached:
                        result.max_depth_reached = depth

                    # Expand frontier
                    if depth < max_depth and len(visited) < node_limit:
                        neighbors = _get_neighbors(node, direction)
                        for neighbor_txid in neighbors:
                            if (
                                neighbor_txid not in visited
                                and len(visited) < node_limit
                            ):
                                visited.add(neighbor_txid)
                                await frontier.put((neighbor_txid, depth + 1))

                    if len(visited) >= node_limit:
                        result.node_limit_hit = True

                if progress_callback:
                    progress_callback(len(result.nodes), node_limit, depth)

            except Exception as exc:
                logger.error("Worker error processing %s: %s", txid, exc)
                async with result_lock:
                    result.nodes[txid] = GraphNode(
                        txid=txid, depth=depth, resolved=False
                    )
                    result.unresolved_count += 1

            finally:
                async with active_lock:
                    active_workers -= 1
                frontier.task_done()

    # Build edges after BFS completes (avoids locking during traversal)
    workers = [asyncio.create_task(worker()) for _ in range(NUM_WORKERS)]
    await asyncio.gather(*workers)

    # Post-process: build edge list
    result.edges = _build_edges(result)

    # Detect dormant address: target appears only in outputs, never as input
    if ADDRESS_PATTERN.match(target.strip()) and result.max_depth_reached == 0:
        target_addr = target.strip()
        spent_from_target = False
        for node in result.nodes.values():
            if not node.resolved:
                continue
            for inp in node.inputs:
                if inp.address == target_addr:
                    spent_from_target = True
                    break
            if spent_from_target:
                break
        if not spent_from_target:
            result.is_dormant = True
            result.dormancy_note = (
                "No outgoing transactions found. "
                "This address has received funds but never spent. "
                "Nothing to trace."
            )

    return result


# ── Input resolution ──────────────────────────────────────────────────────────


async def _resolve_target(
    client: httpx.AsyncClient, target: str
) -> Optional[str]:
    """Resolve a user-provided target to a transaction ID.

    If target is a txid, validate it exists. If it's an address, fetch
    its most recent transaction.
    """
    target = target.strip()

    if TXID_PATTERN.match(target):
        # Validate the txid exists
        tx = await _fetch_tx_with_fallback(client, target)
        return target if tx else None

    if ADDRESS_PATTERN.match(target):
        # Fetch most recent transactions for this address
        txids = await _fetch_address_txids(client, target)
        return txids[0] if txids else None

    return None


async def _fetch_address_txids(
    client: httpx.AsyncClient, address: str, limit: int = 25
) -> list[str]:
    """Fetch recent transaction IDs for a Bitcoin address."""
    # Try mempool.space first
    async with MEMPOOL_LIMITER:
        try:
            resp = await client.get(
                f"{MEMPOOL_BASE}/address/{address}/txs",
                timeout=15.0,
            )
            resp.raise_for_status()
            txs = resp.json()
            return [tx["txid"] for tx in txs[:limit]]
        except Exception:
            pass

    # Fallback to Blockstream
    async with BLOCKSTREAM_LIMITER:
        try:
            resp = await client.get(
                f"{BLOCKSTREAM_BASE}/address/{address}/txs",
                timeout=15.0,
            )
            resp.raise_for_status()
            txs = resp.json()
            return [tx["txid"] for tx in txs[:limit]]
        except Exception:
            return []


# ── Transaction fetching ──────────────────────────────────────────────────────


async def _fetch_tx_with_fallback(
    client: httpx.AsyncClient, txid: str
) -> Optional[dict]:
    """Fetch transaction data, trying mempool.space then Blockstream."""
    data = await _fetch_tx(client, MEMPOOL_BASE, MEMPOOL_LIMITER, txid)
    if data is not None:
        return data

    data = await _fetch_tx(client, BLOCKSTREAM_BASE, BLOCKSTREAM_LIMITER, txid)
    return data


async def _fetch_tx(
    client: httpx.AsyncClient,
    base_url: str,
    limiter,
    txid: str,
) -> Optional[dict]:
    """Fetch a single transaction from an Esplora-compatible API."""
    async with limiter:
        try:
            resp = await client.get(f"{base_url}/tx/{txid}", timeout=15.0)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                await asyncio.sleep(2.0)
                resp = await client.get(f"{base_url}/tx/{txid}", timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPStatusError, httpx.TimeoutException) as exc:
            logger.debug("Fetch tx %s from %s failed: %s", txid, base_url, exc)
            return None
        except Exception as exc:
            logger.debug("Unexpected error fetching %s: %s", txid, exc)
            return None


async def _fetch_outspends_with_fallback(
    client: httpx.AsyncClient, txid: str
) -> Optional[list[dict]]:
    """Fetch outspend data for a transaction's outputs."""
    data = await _fetch_outspends(client, MEMPOOL_BASE, MEMPOOL_LIMITER, txid)
    if data is not None:
        return data

    data = await _fetch_outspends(
        client, BLOCKSTREAM_BASE, BLOCKSTREAM_LIMITER, txid
    )
    return data


async def _fetch_outspends(
    client: httpx.AsyncClient,
    base_url: str,
    limiter,
    txid: str,
) -> Optional[list[dict]]:
    """Fetch outspend data from an Esplora-compatible API."""
    async with limiter:
        try:
            resp = await client.get(
                f"{base_url}/tx/{txid}/outspends", timeout=15.0
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            logger.debug("Fetch outspends %s failed: %s", txid, exc)
            return None


# ── Parsing ───────────────────────────────────────────────────────────────────


def _parse_tx(
    tx_data: dict, depth: int, outspends: Optional[list[dict]] = None
) -> GraphNode:
    """Parse an Esplora API transaction response into a GraphNode."""
    inputs = []
    for vin in tx_data.get("vin", []):
        prevout = vin.get("prevout") or {}
        inputs.append(
            TxInput(
                prev_txid=vin.get("txid", ""),
                prev_vout=vin.get("vout", 0),
                address=prevout.get("scriptpubkey_address"),
                value_sat=prevout.get("value", 0),
                script_type=ScriptType.from_esplora(
                    prevout.get("scriptpubkey_type", "")
                ),
            )
        )

    outputs = []
    vout_list = tx_data.get("vout", [])
    for i, vout in enumerate(vout_list):
        spent = False
        spending_txid = None
        if outspends and i < len(outspends):
            os = outspends[i]
            spent = os.get("spent", False)
            if spent:
                spending_txid = os.get("txid")

        outputs.append(
            TxOutput(
                address=vout.get("scriptpubkey_address"),
                value_sat=vout.get("value", 0),
                script_type=ScriptType.from_esplora(
                    vout.get("scriptpubkey_type", "")
                ),
                spent=spent,
                spending_txid=spending_txid,
            )
        )

    status = tx_data.get("status", {})
    is_coinbase = any(
        vin.get("is_coinbase", False) for vin in tx_data.get("vin", [])
    )

    # RBF: signaled when any non-coinbase input has sequence < 0xFFFFFFFE
    rbf_signaled = any(
        vin.get("sequence", 0xFFFFFFFF) < 0xFFFFFFFE
        for vin in tx_data.get("vin", [])
        if not vin.get("is_coinbase", False)
    )

    return GraphNode(
        txid=tx_data.get("txid", ""),
        inputs=inputs,
        outputs=outputs,
        fee_sat=tx_data.get("fee", 0),
        size_bytes=tx_data.get("size", 0),
        weight=tx_data.get("weight", 0),
        timestamp=status.get("block_time"),
        block_height=status.get("block_height"),
        depth=depth,
        is_coinbase=is_coinbase,
        rbf_signaled=rbf_signaled,
    )


# ── Graph traversal helpers ───────────────────────────────────────────────────


def _get_neighbors(node: GraphNode, direction: str) -> list[str]:
    """Extract neighbor txids based on traversal direction."""
    neighbors = []

    if direction in ("forward", "both"):
        for out in node.outputs:
            if out.spent and out.spending_txid:
                neighbors.append(out.spending_txid)

    if direction in ("backward", "both"):
        for inp in node.inputs:
            if inp.prev_txid and not node.is_coinbase:
                neighbors.append(inp.prev_txid)

    return neighbors


def _build_edges(result: GraphResult) -> list[GraphEdge]:
    """Build edge list from completed node data."""
    edges = []
    for node in result.nodes.values():
        if not node.resolved:
            continue
        for i, out in enumerate(node.outputs):
            if out.spent and out.spending_txid and out.spending_txid in result.nodes:
                edges.append(
                    GraphEdge(
                        from_txid=node.txid,
                        to_txid=out.spending_txid,
                        address=out.address,
                        value_sat=out.value_sat,
                        vout_index=i,
                    )
                )
    return edges
