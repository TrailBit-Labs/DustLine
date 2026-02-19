"""Tests for graph complexity analysis and CoinJoin detection."""

import pytest

from core import (
    GraphNode,
    GraphResult,
    ScriptType,
    TxInput,
    TxOutput,
    TxPattern,
)
from core.complexity import compute_complexity, _is_coinjoin, _classify_tx_pattern


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_graph(nodes: list[GraphNode]) -> GraphResult:
    """Build a minimal GraphResult from a list of nodes."""
    addresses = set()
    for node in nodes:
        for inp in node.inputs:
            if inp.address:
                addresses.add(inp.address)
        for out in node.outputs:
            if out.address:
                addresses.add(out.address)

    return GraphResult(
        root_input="test",
        root_txid=nodes[0].txid if nodes else "",
        nodes={n.txid: n for n in nodes},
        addresses_seen=addresses,
        max_depth_reached=max((n.depth for n in nodes), default=0),
    )


def _simple_node(
    txid: str,
    n_inputs: int = 1,
    n_outputs: int = 2,
    depth: int = 0,
    output_value: int = 50_000_000,
    script_type: ScriptType = ScriptType.P2WPKH,
    attributed: dict | None = None,
) -> GraphNode:
    """Create a simple test node."""
    inputs = [
        TxInput(
            prev_txid=f"prev_{txid}_{i}",
            prev_vout=0,
            address=f"addr_in_{txid}_{i}",
            value_sat=output_value * n_outputs,
            script_type=script_type,
        )
        for i in range(n_inputs)
    ]
    outputs = [
        TxOutput(
            address=f"addr_out_{txid}_{i}",
            value_sat=output_value,
            script_type=script_type,
        )
        for i in range(n_outputs)
    ]
    return GraphNode(
        txid=txid,
        inputs=inputs,
        outputs=outputs,
        depth=depth,
        attributed_entities=attributed or {},
    )


# ── Branch factor tests ──────────────────────────────────────────────────────


def test_branch_factor_single_output():
    """A chain of 1-input-1-output txs has branch factor 1.0."""
    nodes = [_simple_node(f"tx{i}", n_outputs=1, depth=i) for i in range(5)]
    metrics = compute_complexity(_make_graph(nodes))
    assert metrics.avg_branch_factor == 1.0


def test_branch_factor_two_outputs():
    """Standard payment+change txs have branch factor 2.0."""
    nodes = [_simple_node(f"tx{i}", n_outputs=2, depth=i) for i in range(3)]
    metrics = compute_complexity(_make_graph(nodes))
    assert metrics.avg_branch_factor == 2.0


def test_branch_factor_high():
    """Transaction with many outputs raises branch factor."""
    nodes = [_simple_node("tx0", n_outputs=20)]
    metrics = compute_complexity(_make_graph(nodes))
    assert metrics.avg_branch_factor == 20.0
    assert metrics.max_branch_factor == 20


# ── Attribution rate tests ────────────────────────────────────────────────────


def test_attribution_rate_fully_attributed():
    """All addresses attributed -> rate ~1.0."""
    node = _simple_node("tx0", n_inputs=1, n_outputs=2)
    # Attribute all addresses in this node
    all_addrs = {}
    for inp in node.inputs:
        if inp.address:
            all_addrs[inp.address] = "Exchange"
    for out in node.outputs:
        if out.address:
            all_addrs[out.address] = "Exchange"
    node.attributed_entities = all_addrs

    metrics = compute_complexity(_make_graph([node]))
    assert metrics.attribution_rate == 1.0


def test_attribution_rate_none():
    """No addresses attributed -> rate 0.0."""
    node = _simple_node("tx0")
    metrics = compute_complexity(_make_graph([node]))
    assert metrics.attribution_rate == 0.0


def test_attribution_rate_partial():
    """Some addresses attributed -> rate between 0 and 1."""
    node = _simple_node("tx0", n_inputs=2, n_outputs=3)
    # Attribute just one address
    addr = node.outputs[0].address
    node.attributed_entities = {addr: "Binance"}

    metrics = compute_complexity(_make_graph([node]))
    assert 0 < metrics.attribution_rate < 1


# ── CoinJoin detection tests ─────────────────────────────────────────────────


def test_coinjoin_wasabi_v1():
    """5+ outputs at 0.1 BTC = Wasabi v1 CoinJoin."""
    outputs = [
        TxOutput(address=f"addr{i}", value_sat=10_000_000, script_type=ScriptType.P2WPKH)
        for i in range(10)
    ]
    # Add change outputs
    outputs.extend([
        TxOutput(address=f"change{i}", value_sat=150_000, script_type=ScriptType.P2WPKH)
        for i in range(5)
    ])
    node = GraphNode(txid="wasabi_cj", outputs=outputs)
    assert _is_coinjoin(node) is True


def test_coinjoin_whirlpool():
    """5 equal outputs at a Whirlpool denomination."""
    outputs = [
        TxOutput(address=f"addr{i}", value_sat=1_000_000, script_type=ScriptType.P2WPKH)
        for i in range(5)
    ]
    node = GraphNode(txid="whirlpool_cj", outputs=outputs)
    assert _is_coinjoin(node) is True


def test_not_coinjoin_payment_plus_change():
    """Normal 2-output tx (payment + change) is NOT CoinJoin."""
    outputs = [
        TxOutput(address="recipient", value_sat=50_000_000, script_type=ScriptType.P2WPKH),
        TxOutput(address="change", value_sat=49_990_000, script_type=ScriptType.P2WPKH),
    ]
    node = GraphNode(txid="normal_tx", outputs=outputs)
    assert _is_coinjoin(node) is False


def test_not_coinjoin_batch_payment():
    """Exchange batch payment (many outputs, different amounts) is NOT CoinJoin."""
    outputs = [
        TxOutput(address=f"addr{i}", value_sat=i * 100_000 + 50_000, script_type=ScriptType.P2WPKH)
        for i in range(20)
    ]
    node = GraphNode(txid="batch_payment", outputs=outputs)
    assert _is_coinjoin(node) is False


def test_not_coinjoin_consolidation():
    """Consolidation tx (many inputs, 1 output) is NOT CoinJoin."""
    outputs = [
        TxOutput(address="consolidated", value_sat=100_000_000, script_type=ScriptType.P2WPKH),
    ]
    node = GraphNode(txid="consolidation", outputs=outputs)
    assert _is_coinjoin(node) is False


def test_coinjoin_detected_in_graph():
    """CoinJoin detection propagates to graph-level metrics."""
    # Mix of normal and CoinJoin transactions
    normal = _simple_node("normal", n_outputs=2)
    cj_outputs = [
        TxOutput(address=f"cj{i}", value_sat=10_000_000, script_type=ScriptType.P2WPKH)
        for i in range(8)
    ]
    coinjoin = GraphNode(txid="coinjoin", outputs=cj_outputs, depth=1)

    metrics = compute_complexity(_make_graph([normal, coinjoin]))
    assert metrics.coinjoin_detected is True
    assert metrics.mixing_signals == 1
    assert "coinjoin" in metrics.mixing_txids


# ── Taproot ratio tests ──────────────────────────────────────────────────────


def test_taproot_ratio_all_taproot():
    """All P2TR addresses -> taproot ratio ~1.0."""
    node = _simple_node("tx0", script_type=ScriptType.P2TR)
    metrics = compute_complexity(_make_graph([node]))
    assert metrics.taproot_ratio == 1.0


def test_taproot_ratio_none():
    """No P2TR addresses -> taproot ratio 0.0."""
    node = _simple_node("tx0", script_type=ScriptType.P2WPKH)
    metrics = compute_complexity(_make_graph([node]))
    assert metrics.taproot_ratio == 0.0


# ── Unresolved paths tests ───────────────────────────────────────────────────


def test_unresolved_paths_counted():
    """Unresolved nodes are counted correctly."""
    resolved = _simple_node("ok", depth=0)
    unresolved = GraphNode(txid="fail", depth=1, resolved=False)

    metrics = compute_complexity(_make_graph([resolved, unresolved]))
    assert metrics.unresolved_paths == 1


# ── Empty graph ───────────────────────────────────────────────────────────────


def test_empty_graph():
    """Empty graph returns zeroed metrics without errors."""
    graph = GraphResult(root_input="test", root_txid="")
    metrics = compute_complexity(graph)
    assert metrics.node_count == 0
    assert metrics.attribution_rate == 0.0


# ── Fan-in tests ────────────────────────────────────────────────────────────


def test_fan_in_single_input():
    """Standard 1-input txs have fan-in 1.0."""
    nodes = [_simple_node(f"tx{i}", n_inputs=1, depth=i) for i in range(3)]
    metrics = compute_complexity(_make_graph(nodes))
    assert metrics.avg_fan_in == 1.0
    assert metrics.max_fan_in == 1


def test_fan_in_consolidation():
    """Consolidation tx with many inputs raises fan-in."""
    node = _simple_node("consolidate", n_inputs=20, n_outputs=1)
    metrics = compute_complexity(_make_graph([node]))
    assert metrics.avg_fan_in == 20.0
    assert metrics.max_fan_in == 20


def test_fan_in_excludes_coinbase():
    """Coinbase transactions are excluded from fan-in calculation."""
    coinbase = GraphNode(
        txid="coinbase_tx",
        inputs=[TxInput(prev_txid="0" * 64, prev_vout=0, address=None, value_sat=0, script_type=ScriptType.UNKNOWN)],
        outputs=[TxOutput(address="miner", value_sat=625_000_000, script_type=ScriptType.P2WPKH)],
        is_coinbase=True,
    )
    normal = _simple_node("tx1", n_inputs=3, depth=1)
    metrics = compute_complexity(_make_graph([coinbase, normal]))
    assert metrics.avg_fan_in == 3.0  # Coinbase excluded, only normal counted


# ── Pattern classification tests ────────────────────────────────────────────


def test_classify_consolidation():
    """Many inputs, few outputs -> CONSOLIDATION."""
    node = _simple_node("tx0", n_inputs=10, n_outputs=1)
    pattern, detail = _classify_tx_pattern(node, is_coinjoin=False)
    assert pattern == TxPattern.CONSOLIDATION
    assert "10-in" in detail
    assert "1-out" in detail


def test_classify_peel_chain():
    """1-2 inputs, 2 outputs -> PEEL_CHAIN."""
    node = _simple_node("tx0", n_inputs=1, n_outputs=2)
    pattern, _ = _classify_tx_pattern(node, is_coinjoin=False)
    assert pattern == TxPattern.PEEL_CHAIN


def test_classify_fan_out():
    """Few inputs, many outputs -> FAN_OUT."""
    node = _simple_node("tx0", n_inputs=1, n_outputs=10)
    pattern, _ = _classify_tx_pattern(node, is_coinjoin=False)
    assert pattern == TxPattern.FAN_OUT


def test_classify_simple():
    """1 input, 1 output -> SIMPLE."""
    node = _simple_node("tx0", n_inputs=1, n_outputs=1)
    pattern, _ = _classify_tx_pattern(node, is_coinjoin=False)
    assert pattern == TxPattern.SIMPLE


def test_classify_coinjoin_overrides_shape():
    """CoinJoin flag overrides structural classification."""
    # This node has consolidation shape (10-in, 1-out)
    # but CoinJoin should take priority
    node = _simple_node("tx0", n_inputs=10, n_outputs=1)
    pattern, _ = _classify_tx_pattern(node, is_coinjoin=True)
    assert pattern == TxPattern.COINJOIN


def test_root_pattern_in_metrics():
    """Pattern classification propagates to ComplexityMetrics."""
    # Consolidation: 10 inputs, 1 output
    node = _simple_node("root_tx", n_inputs=10, n_outputs=1)
    metrics = compute_complexity(_make_graph([node]))
    assert metrics.root_pattern == TxPattern.CONSOLIDATION
    assert "10-in" in metrics.root_pattern_detail
