"""Graph complexity analysis and CoinJoin detection.

Pure computation — no I/O, no async. Operates on a completed GraphResult
to produce ComplexityMetrics that drive the cost model.
"""

from collections import Counter

from core import (
    ComplexityMetrics,
    GraphNode,
    GraphResult,
    ScriptType,
    TxPattern,
)

# Known CoinJoin denomination patterns (satoshis)
WASABI_V1_DENOMINATIONS = {10_000_000}  # 0.1 BTC
WHIRLPOOL_DENOMINATIONS = {
    100_000,  # 0.001 BTC
    1_000_000,  # 0.01 BTC
    2_500_000,  # 0.025 BTC (Ashigaru)
    5_000_000,  # 0.05 BTC
    25_000_000,  # 0.25 BTC (Ashigaru)
    50_000_000,  # 0.5 BTC
}
ALL_KNOWN_DENOMINATIONS = WASABI_V1_DENOMINATIONS | WHIRLPOOL_DENOMINATIONS

# Minimum equal outputs to suspect CoinJoin (below this, likely normal tx)
MIN_EQUAL_OUTPUTS_FOR_COINJOIN = 5


def compute_complexity(graph: GraphResult) -> ComplexityMetrics:
    """Compute all complexity metrics from a traversed graph."""
    nodes = list(graph.nodes.values())
    if not nodes:
        return _empty_metrics()

    # Basic counts
    node_count = len(nodes)
    edge_count = len(graph.edges)
    unique_addresses = len(graph.addresses_seen)
    max_depth = graph.max_depth_reached

    # Branch factor (average outputs per transaction — forward fan-out)
    output_counts = [len(n.outputs) for n in nodes if n.resolved]
    avg_branch = sum(output_counts) / len(output_counts) if output_counts else 1.0
    max_branch = max(output_counts) if output_counts else 1

    # Fan-in (average inputs per transaction — backward complexity)
    input_counts = [
        len(n.inputs) for n in nodes if n.resolved and not n.is_coinbase
    ]
    avg_fan_in = sum(input_counts) / len(input_counts) if input_counts else 1.0
    max_fan_in = max(input_counts) if input_counts else 1

    # Attribution rate
    attributed = set()
    for node in nodes:
        attributed.update(node.attributed_entities.keys())
    attributed_count = len(attributed)
    total_addr = max(unique_addresses, 1)
    attribution_rate = attributed_count / total_addr

    # Coverage: use attribution summary if available, else legacy calculation
    if graph.attribution_summary:
        # Three-tier pipeline: Tier 1 checks all addresses instantly
        addresses_checked = graph.attribution_summary.total_addresses
        unattributed_addresses = (
            graph.attribution_summary.total_addresses
            - graph.attribution_summary.attributed_count
        )
    else:
        # Legacy: local DB checks all addresses; WE checks a subset
        addresses_checked = attributed_count + graph.we_addresses_queried
        unattributed_addresses = unique_addresses - attributed_count

    # CoinJoin detection
    mixing_txids = []
    for node in nodes:
        if node.resolved and _is_coinjoin(node):
            mixing_txids.append(node.txid)
    mixing_signals = len(mixing_txids)
    coinjoin_detected = mixing_signals > 0

    # Root transaction pattern classification
    root_node = graph.nodes.get(graph.root_txid)
    root_pattern = None
    root_pattern_detail = ""
    if root_node and root_node.resolved:
        is_root_coinjoin = coinjoin_detected and root_node.txid in mixing_txids
        root_pattern, root_pattern_detail = _classify_tx_pattern(
            root_node, is_root_coinjoin
        )

    # Taproot ratio
    all_script_types = []
    for node in nodes:
        if not node.resolved:
            continue
        for inp in node.inputs:
            if inp.address:
                all_script_types.append(inp.script_type)
        for out in node.outputs:
            if out.address:
                all_script_types.append(out.script_type)
    taproot_count = sum(1 for st in all_script_types if st == ScriptType.P2TR)
    taproot_ratio = taproot_count / len(all_script_types) if all_script_types else 0.0

    # Script type breakdown
    script_counts: dict[str, int] = {}
    for st in all_script_types:
        script_counts[st.value] = script_counts.get(st.value, 0) + 1

    # Unresolved paths
    unresolved = sum(1 for n in nodes if not n.resolved)

    # Total value flow
    total_value = sum(
        out.value_sat
        for node in nodes
        if node.resolved
        for out in node.outputs
    )

    # Sources exhausted: WE checked all unmatched (no capping), or WE was skipped
    sources_exhausted = (
        graph.we_addresses_queried >= graph.we_addresses_total_unmatched
    )

    return ComplexityMetrics(
        node_count=node_count,
        edge_count=edge_count,
        unique_addresses=unique_addresses,
        max_depth=max_depth,
        avg_branch_factor=round(avg_branch, 2),
        max_branch_factor=max_branch,
        attribution_rate=round(attribution_rate, 4),
        attributed_addresses=attributed_count,
        total_addresses=unique_addresses,
        mixing_signals=mixing_signals,
        mixing_txids=mixing_txids,
        coinjoin_detected=coinjoin_detected,
        taproot_ratio=round(taproot_ratio, 4),
        unresolved_paths=unresolved,
        addresses_checked=addresses_checked,
        unattributed_addresses=unattributed_addresses,
        sources_exhausted=sources_exhausted,
        avg_fan_in=round(avg_fan_in, 2),
        max_fan_in=max_fan_in,
        root_pattern=root_pattern,
        root_pattern_detail=root_pattern_detail,
        script_type_counts=script_counts,
        total_value_sat=total_value,
    )


def _is_coinjoin(node: GraphNode) -> bool:
    """Detect whether a transaction is likely a CoinJoin.

    Heuristics:
    1. Known denomination match: many equal outputs at Wasabi/Whirlpool amounts
    2. Generic equal-output: many outputs of the same value (unknown coordinator)
    3. NOT triggered by: 2-output txs (normal payment+change),
       consolidation (many inputs, 1-2 outputs), or exchange batch payments
       (many outputs at different amounts)
    """
    outputs = node.outputs
    if len(outputs) < MIN_EQUAL_OUTPUTS_FOR_COINJOIN:
        return False

    # Count outputs by value (ignore zero-value / OP_RETURN)
    value_counts = Counter(
        out.value_sat for out in outputs if out.value_sat > 0
    )

    if not value_counts:
        return False

    # Check 1: Known denomination match
    for value, count in value_counts.items():
        if count >= 3 and value in ALL_KNOWN_DENOMINATIONS:
            return True

    # Check 2: Many equal outputs at any value (Wasabi v2 or unknown coordinator)
    # Require at least 5 outputs at the same value, AND that group must be
    # the majority of outputs (>50%) to exclude batch payments
    most_common_value, most_common_count = value_counts.most_common(1)[0]
    if most_common_count >= MIN_EQUAL_OUTPUTS_FOR_COINJOIN:
        equal_ratio = most_common_count / len(outputs)
        if equal_ratio > 0.5:
            return True

    # Check 3: Wasabi v2 style — multiple groups of equal outputs
    # (several denominations, each with 3+ equal outputs)
    equal_groups = sum(1 for count in value_counts.values() if count >= 3)
    if equal_groups >= 3:
        return True

    return False


def _classify_tx_pattern(
    node: GraphNode, is_coinjoin: bool
) -> tuple[TxPattern, str]:
    """Classify a transaction into a common Bitcoin pattern.

    Patterns:
    - COINJOIN: overrides shape if CoinJoin detected
    - CONSOLIDATION: >= 5 inputs, <= 2 outputs
    - PEEL_CHAIN: <= 2 inputs, exactly 2 outputs (payment + change)
    - FAN_OUT: <= 3 inputs, >= 5 outputs (batch payment)
    - SIMPLE: anything else
    """
    n_in = len(node.inputs)
    n_out = len(node.outputs)
    detail = f"{n_in}-in \u2192 {n_out}-out"

    if is_coinjoin:
        return TxPattern.COINJOIN, detail
    if n_in >= 5 and n_out <= 2:
        return TxPattern.CONSOLIDATION, detail
    if n_in <= 2 and n_out == 2:
        return TxPattern.PEEL_CHAIN, detail
    if n_in <= 3 and n_out >= 5:
        return TxPattern.FAN_OUT, detail
    return TxPattern.SIMPLE, detail


def _empty_metrics() -> ComplexityMetrics:
    """Return zeroed-out metrics for an empty graph."""
    return ComplexityMetrics(
        node_count=0,
        edge_count=0,
        unique_addresses=0,
        max_depth=0,
        avg_branch_factor=0.0,
        max_branch_factor=0,
        attribution_rate=0.0,
        attributed_addresses=0,
        total_addresses=0,
        mixing_signals=0,
    )
