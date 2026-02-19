"""Output formatting for terminal (Rich) and JSON.

Renders GraphResult, ComplexityMetrics, and CostEstimate into
the human-readable terminal format defined in the spec, or as
structured JSON for programmatic consumption.
"""

from __future__ import annotations

import json
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core import (
    AttributionSummary,
    ComplexityMetrics,
    CostEstimate,
    GraphResult,
    PrivacyFloor,
    TxPattern,
)


def render_terminal(
    console: Console,
    graph: GraphResult,
    metrics: ComplexityMetrics,
    estimate: CostEstimate,
    verbose: bool = False,
    methodology: bool = False,
) -> None:
    """Render full analysis to terminal using Rich."""

    # Header
    console.print()
    console.print(
        "[bold]DustLine v1.0[/bold] \u2014 Economic Privacy Estimator",
        style="bright_white",
    )
    console.print("\u2501" * 50, style="dim")
    console.print()

    # Target info
    target = graph.root_input
    if len(target) > 50:
        target = target[:24] + ".." + target[-24:]
    console.print(f"  [dim]Target:[/dim]          {target}")
    console.print(f"  [dim]Root txid:[/dim]       {graph.root_txid[:16]}...")
    if graph.requested_max_depth > metrics.max_depth:
        console.print(
            f"  [dim]Depth analyzed:[/dim]  {metrics.max_depth} / "
            f"{graph.requested_max_depth} requested"
        )
        if metrics.max_depth == 0:
            console.print(
                "  [dim]  (no outgoing transactions from root \u2014 "
                "traversal could not expand)[/dim]"
            )
    else:
        console.print(f"  [dim]Depth analyzed:[/dim]  {metrics.max_depth} hops")
    console.print(f"  [dim]Nodes traversed:[/dim] {metrics.node_count}")
    if graph.node_limit_hit:
        console.print("  [yellow]\u26a0 Node limit reached[/yellow]")
    console.print()

    # Dormant address: short-circuit before analysis sections
    if graph.is_dormant:
        console.print(f"  [bold yellow]{graph.dormancy_note}[/bold yellow]")
        console.print()
        console.print("  [dim]No cost estimate applicable.[/dim]")
        console.print()
        return

    # Graph Complexity section
    console.print("[bold]GRAPH COMPLEXITY[/bold]")
    _branch_desc = _describe_branch_factor(metrics.avg_branch_factor)
    console.print(f"  Branch factor:      {metrics.avg_branch_factor} ({_branch_desc})")
    if metrics.avg_fan_in > 1.5:
        _fan_desc = _describe_fan_in(metrics.avg_fan_in)
        console.print(f"  Fan-in (backward):  {metrics.avg_fan_in} ({_fan_desc})")
    # Attribution rate with WE coverage context
    attr_line = (
        f"  Attribution rate:   {metrics.attribution_rate:.0%} "
        f"({metrics.attributed_addresses}/{metrics.total_addresses} addresses)"
    )
    we_skipped = graph.we_addresses_total_unmatched - graph.we_addresses_queried
    if we_skipped > 0:
        attr_line += (
            f"  [dim](checked {graph.we_addresses_queried}"
            f"/{graph.we_addresses_total_unmatched} via WE)[/dim]"
        )
    console.print(attr_line)
    _mixing = "Yes" if metrics.coinjoin_detected else "No"
    if metrics.coinjoin_detected:
        console.print(f"  [bold red]Mixing detected:    {_mixing} ({metrics.mixing_signals} txs)[/bold red]")
    else:
        console.print(f"  Mixing detected:    {_mixing}")
    console.print(f"  Taproot ratio:      {metrics.taproot_ratio:.0%}")
    if metrics.unresolved_paths > 0:
        console.print(f"  Fetch failures:     {metrics.unresolved_paths}")
    if metrics.unattributed_addresses > 0:
        unattr_pct = metrics.unattributed_addresses / max(metrics.total_addresses, 1)
        console.print(
            f"  Unattributed:       {metrics.unattributed_addresses} "
            f"({unattr_pct:.0%} of graph has no known entity label)"
        )
    console.print()

    # Attribution Sources section (only when summary available)
    if graph.attribution_summary:
        _render_attribution_sources(console, graph)

    # Known Entities listing (only when attributions found)
    if graph.attribution_results:
        _render_known_entities(console, graph)

    # Pattern Analysis section (only when a pattern is detected)
    if metrics.root_pattern and metrics.root_pattern != TxPattern.SIMPLE:
        console.print("[bold]PATTERN ANALYSIS[/bold]")
        console.print(
            f"  Pattern detected:   {metrics.root_pattern.label} "
            f"({metrics.root_pattern_detail})"
        )
        root_node = graph.nodes.get(graph.root_txid)
        if root_node and root_node.rbf_signaled:
            console.print("  RBF signaled:       Yes")
        console.print(f"  {_pattern_note(metrics.root_pattern)}")
        console.print()

    # Time Estimate section
    console.print("[bold]TIME ESTIMATE[/bold]")
    _base_desc = _describe_base_time(estimate.base_hours_per_hop)
    console.print(f"  Per-hop base time:  {_format_hours(estimate.base_hours_per_hop)} ({_base_desc})")

    # Show multipliers if any are active
    if estimate.mixing_multiplier > 1:
        console.print(f"  [red]Mixing multiplier:  \u00d7{estimate.mixing_multiplier}[/red]")
    if estimate.branching_multiplier > 1:
        console.print(f"  Branch multiplier:  \u00d7{estimate.branching_multiplier}")
    if estimate.taproot_multiplier > 1:
        console.print(f"  Taproot multiplier: \u00d7{estimate.taproot_multiplier}")
    if estimate.fan_in_multiplier > 1:
        console.print(f"  Fan-in multiplier:  \u00d7{estimate.fan_in_multiplier}")

    tier_ref = estimate.tiers[1]  # Senior specialist as reference
    console.print(
        f"  Total analyst hours: ~{tier_ref.estimated_hours_low:.0f}\u2013"
        f"{tier_ref.estimated_hours_high:.0f} hours"
    )
    if estimate.unresolved_hours > 0:
        console.print(
            f"  [yellow]\u26a0 {metrics.unresolved_paths} unresolved paths add "
            f"~{estimate.unresolved_hours:.0f} hours if pursued[/yellow]"
        )
    console.print()

    # Cost Estimate section
    console.print("[bold]COST ESTIMATE[/bold]")
    cost_table = Table(show_header=False, box=None, padding=(0, 2, 0, 2))
    cost_table.add_column("Tier", style="dim")
    cost_table.add_column("Range", justify="right")

    for tier in estimate.tiers:
        rate_str = f"(${tier.hourly_rate:.0f}/hr)"
        if tier.tooling_overhead > 0:
            rate_str = f"(${tier.hourly_rate:.0f}+${tier.tooling_overhead:.0f}/hr)"
        cost_table.add_row(
            f"{tier.tier_name} {rate_str}",
            f"${tier.total_low:,.0f} \u2013 ${tier.total_high:,.0f}",
        )

    console.print(cost_table)
    console.print()

    if estimate.minimum_case_threshold_note:
        console.print(f"  [dim]\u26a0 {estimate.minimum_case_threshold_note}[/dim]")
        console.print()

    # Privacy Floor section
    floor = estimate.privacy_floor
    floor_style = _floor_style(floor)
    console.print("[bold]ECONOMIC PRIVACY FLOOR[/bold]")
    console.print(
        f"  {floor.emoji} [bold {floor_style}]{floor.label}[/bold {floor_style}]"
    )
    console.print(f"  {estimate.privacy_floor_summary}")
    console.print()

    # Confidence
    conf_style = {"high": "green", "moderate": "yellow", "low": "red", "very low": "bold red"}
    _cs = conf_style.get(estimate.confidence, "white")
    console.print(
        f"  [dim]Confidence:[/dim] [{_cs}]{estimate.confidence}[/{_cs}]"
    )
    if estimate.confidence_note:
        console.print(f"  [yellow]\u26a0 {estimate.confidence_note}[/yellow]")
    console.print()

    # Verbose: per-hop breakdown
    if verbose:
        _render_verbose(console, graph, metrics)

    # Methodology
    if methodology:
        _render_methodology(console)

    # Warnings
    if graph.warnings:
        console.print("[bold yellow]WARNINGS[/bold yellow]")
        for warning in graph.warnings:
            console.print(f"  \u26a0 {warning}")
        console.print()

    # Data sources footer
    console.print("[dim]METHODOLOGY[/dim]")
    sources_str = (
        ", ".join(graph.attribution_summary.sources_used)
        if graph.attribution_summary
        else "local entity database"
    )
    console.print(f"  [dim]Based on: {sources_str}[/dim]")
    console.print(f"  [dim]Rate source: ExpertPages 2024 Expert Witness Survey (n=1,600+)[/dim]")
    console.print(f"  [dim]Time model: TrailBit Labs practitioner estimates[/dim]")
    console.print()


def render_json(
    graph: GraphResult,
    metrics: ComplexityMetrics,
    estimate: CostEstimate,
) -> None:
    """Render analysis as JSON to stdout."""
    output = {
        "input": graph.root_input,
        "root_txid": graph.root_txid,
        "depth": metrics.max_depth,
        "requested_depth": graph.requested_max_depth,
        "is_dormant": graph.is_dormant,
        "dormancy_note": graph.dormancy_note if graph.is_dormant else None,
        "graph": {
            "node_count": metrics.node_count,
            "edge_count": metrics.edge_count,
            "unique_addresses": metrics.unique_addresses,
            "branch_factor": metrics.avg_branch_factor,
            "avg_fan_in": metrics.avg_fan_in,
            "max_fan_in": metrics.max_fan_in,
            "root_pattern": metrics.root_pattern.value if metrics.root_pattern else None,
            "root_pattern_detail": metrics.root_pattern_detail or None,
            "attribution_rate": metrics.attribution_rate,
            "addresses_checked": metrics.addresses_checked,
            "unattributed_addresses": metrics.unattributed_addresses,
            "we_addresses_queried": graph.we_addresses_queried,
            "we_addresses_skipped": max(0, graph.we_addresses_total_unmatched - graph.we_addresses_queried),
            "mixing_detected": metrics.coinjoin_detected,
            "mixing_signals": metrics.mixing_signals,
            "taproot_ratio": metrics.taproot_ratio,
            "fetch_failures": metrics.unresolved_paths,
            "node_limit_hit": graph.node_limit_hit,
        },
        "time_estimate": {
            "base_hours_per_hop": estimate.base_hours_per_hop,
            "total_hops": estimate.total_hops,
            "multipliers": {
                "mixing": estimate.mixing_multiplier,
                "branching": estimate.branching_multiplier,
                "taproot": estimate.taproot_multiplier,
                "fan_in": estimate.fan_in_multiplier,
            },
            "unresolved_additional_hours": estimate.unresolved_hours,
            "confidence": estimate.confidence,
            "confidence_note": estimate.confidence_note or None,
        },
        "cost_estimate": {},
        "privacy_floor": {
            "rating": estimate.privacy_floor.value,
            "label": f"{estimate.privacy_floor.emoji} {estimate.privacy_floor.label}",
            "summary": estimate.privacy_floor_summary,
        },
        "attribution": _build_attribution_json(graph),
        "warnings": graph.warnings,
    }

    for tier in estimate.tiers:
        key = tier.tier_name.lower().replace(" ", "_")
        output["cost_estimate"][key] = {
            "hourly_rate": tier.hourly_rate,
            "tooling_overhead": tier.tooling_overhead,
            "hours_low": tier.estimated_hours_low,
            "hours_high": tier.estimated_hours_high,
            "total_low": tier.total_low,
            "total_high": tier.total_high,
        }

    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    print()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _describe_branch_factor(bf: float) -> str:
    if bf <= 2.0:
        return "low fragmentation"
    elif bf <= 5.0:
        return "moderate fragmentation"
    else:
        return "high fragmentation"


def _describe_fan_in(fi: float) -> str:
    if fi <= 2.0:
        return "low consolidation"
    elif fi <= 5.0:
        return "moderate consolidation"
    else:
        return "heavy consolidation"


def _pattern_note(pattern: TxPattern) -> str:
    notes = {
        TxPattern.CONSOLIDATION: (
            "[dim]Consolidation merges many UTXOs into one.[/dim]\n"
            "  [yellow]\u26a0 Cost estimate reflects forward tracing only. "
            "Full investigation requires backward tracing all input addresses. "
            "Actual cost may be significantly higher.[/yellow]"
        ),
        TxPattern.PEEL_CHAIN: (
            "[dim]Peel chain: likely a payment + change output. "
            "One hop usually resolves the recipient.[/dim]"
        ),
        TxPattern.FAN_OUT: (
            "[dim]Fan-out: batch payment or distribution. "
            "Each output is a separate trace path.[/dim]"
        ),
        TxPattern.COINJOIN: (
            "[dim]CoinJoin: equal-value outputs obscure the link "
            "between inputs and outputs.[/dim]"
        ),
        TxPattern.SIMPLE: "[dim]Standard transaction pattern.[/dim]",
    }
    return notes.get(pattern, "")


def _describe_base_time(hours: float) -> str:
    if hours <= 0.25:
        return "fast \u2014 most nodes known"
    elif hours <= 1.0:
        return "moderate attribution"
    elif hours <= 4.0:
        return "slow \u2014 few anchors"
    else:
        return "very slow \u2014 essentially unattributed"


def _format_hours(hours: float) -> str:
    if hours < 1:
        return f"{hours * 60:.0f} min"
    else:
        return f"{hours:.1f} hrs"


def _floor_style(floor: PrivacyFloor) -> str:
    return {
        PrivacyFloor.TRACEABLE: "red",
        PrivacyFloor.COSTLY: "yellow",
        PrivacyFloor.EXPENSIVE: "bright_yellow",
        PrivacyFloor.HIGH_FLOOR: "green",
        PrivacyFloor.IMPRACTICAL: "magenta",
    }.get(floor, "white")


def _render_attribution_sources(
    console: Console,
    graph: GraphResult,
) -> None:
    """Render ATTRIBUTION SOURCES section."""
    summary = graph.attribution_summary
    console.print("[bold]ATTRIBUTION SOURCES[/bold]")

    # Local database line
    local_count = summary.by_source.get("local_db", 0)
    if local_count > 0:
        cat_parts = []
        for cat, count in sorted(summary.by_category.items()):
            cat_parts.append(f"{cat}: {count}")
        cat_str = f" ({', '.join(cat_parts)})" if cat_parts else ""
        console.print(f"  Local database:      {local_count} matches{cat_str}")
    else:
        console.print("  Local database:      0 matches")

    # WalletExplorer line
    if "walletexplorer" in summary.sources_used:
        we_count = summary.by_source.get("walletexplorer", 0)
        we_queried = graph.we_addresses_queried
        we_total = graph.we_addresses_total_unmatched
        console.print(
            f"  WalletExplorer:      {we_count} matches "
            f"(queried {we_queried}/{we_total} unmatched)"
        )

    # Arkham Intelligence line
    if "arkham" in summary.sources_used:
        ark_count = summary.by_source.get("arkham", 0)
        console.print(f"  Arkham Intelligence: {ark_count} matches")

    # Total coverage
    console.print(
        f"  Total coverage:      {summary.attributed_count}/{summary.total_addresses} "
        f"addresses ({summary.coverage_rate:.0%})"
    )
    console.print()


def _render_known_entities(
    console: Console,
    graph: GraphResult,
) -> None:
    """Render KNOWN ENTITIES section listing attributed addresses by entity."""
    MAX_DISPLAY = 30

    # Group by entity name
    entities: dict[str, list] = {}
    for ar in graph.attribution_results:
        entities.setdefault(ar.entity, []).append(ar)

    # Sort by count (descending), then alphabetically
    sorted_entities = sorted(entities.items(), key=lambda x: (-len(x[1]), x[0]))

    console.print("[bold]KNOWN ENTITIES[/bold]")

    shown = 0
    for entity_name, results in sorted_entities:
        if shown >= MAX_DISPLAY:
            remaining = sum(len(r) for _, r in sorted_entities) - shown
            console.print(f"  [dim]... and {remaining} more attributed addresses[/dim]")
            break

        category = results[0].category
        cat_str = f" ({category})" if category else ""
        console.print(f"  [bold]{entity_name}[/bold][dim]{cat_str}[/dim]")

        for ar in results:
            if shown >= MAX_DISPLAY:
                leftover = len(results) - results.index(ar)
                console.print(f"    [dim]... +{leftover} more[/dim]")
                break
            console.print(f"    [dim]{ar.address}[/dim]")
            shown += 1

    console.print()


def _build_attribution_json(graph: GraphResult) -> dict | None:
    """Build attribution section for JSON output."""
    if not graph.attribution_summary:
        return None
    summary = graph.attribution_summary
    per_address = {}
    for ar in graph.attribution_results:
        per_address[ar.address] = {
            "entity": ar.entity,
            "source": ar.source,
            "category": ar.category,
            "confidence": ar.confidence,
        }
    return {
        "total_addresses": summary.total_addresses,
        "attributed_count": summary.attributed_count,
        "coverage_rate": round(summary.coverage_rate, 4),
        "by_source": summary.by_source,
        "by_category": summary.by_category,
        "sources_used": summary.sources_used,
        "addresses": per_address,
    }


def _render_verbose(
    console: Console,
    graph: GraphResult,
    metrics: ComplexityMetrics,
) -> None:
    """Render per-hop breakdown in verbose mode."""
    console.print("[bold]PER-HOP BREAKDOWN[/bold]")

    # Group nodes by depth
    by_depth: dict[int, list] = {}
    for node in graph.nodes.values():
        by_depth.setdefault(node.depth, []).append(node)

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Depth", justify="right", style="dim")
    table.add_column("Nodes", justify="right")
    table.add_column("Attributed", justify="right")
    table.add_column("Unresolved", justify="right")
    table.add_column("Mixing", justify="right")

    for depth in sorted(by_depth.keys()):
        nodes = by_depth[depth]
        n_count = len(nodes)
        n_attr = sum(
            1 for n in nodes if n.attributed_entities and n.resolved
        )
        n_unresolved = sum(1 for n in nodes if not n.resolved)
        n_mixing = sum(
            1 for n in nodes if n.txid in metrics.mixing_txids
        )
        table.add_row(
            str(depth),
            str(n_count),
            str(n_attr),
            str(n_unresolved) if n_unresolved else "\u2014",
            str(n_mixing) if n_mixing else "\u2014",
        )

    console.print(table)
    console.print()


def _render_methodology(console: Console) -> None:
    """Render methodology citations."""
    console.print("[bold]METHODOLOGY & CITATIONS[/bold]")
    console.print()
    citations = [
        ("Analyst rates", "ExpertPages 2024 Expert Witness Fees Survey", "Median $451/hr (n=1,600+)"),
        ("Analyst rates", "SEAK 2024 Expert Witness Survey", "Median file review $450/hr"),
        ("Time model", "TrailBit Labs practitioner estimates", "900 days / 6,500 nodes / incomplete (2023)"),
        ("Time model", "TrailBit Labs practitioner estimates", "5 hops, high attribution: ~1 hour (2024)"),
        ("Case threshold", "A&D Forensics (public)", "Minimum investigation value: $5,000"),
        ("Expert rates", "Aaron Hall Law (blockchain forensic)", "Several hundred to several thousand/hr (2024)"),
    ]

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Category", style="dim")
    table.add_column("Source")
    table.add_column("Data Point")

    for cat, source, point in citations:
        table.add_row(cat, source, point)

    console.print(table)
    console.print()
    console.print("[dim]  Model limitations:[/dim]")
    console.print("[dim]  - Attribution databases are incomplete and biased toward well-known exchanges[/dim]")
    console.print("[dim]  - Time estimates are practitioner-derived, not peer-reviewed[/dim]")
    console.print("[dim]  - ML-based tracing automation is not modeled (costs may be lower)[/dim]")
    console.print("[dim]  - Lightning Network channels (off-chain) not analyzed[/dim]")
    console.print("[dim]  - Cross-chain hops flagged but not traced[/dim]")
    console.print("[dim]  - The economic floor will erode as forensic tooling improves[/dim]")
    console.print()
