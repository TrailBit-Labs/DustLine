"""DustLine — Bitcoin forensic cost estimator.

Estimates the real-world cost of tracing a Bitcoin address or transaction
by analyzing graph complexity, entity attribution, and forensic time models.
"""

from __future__ import annotations

import asyncio
import logging
import sys

import click
import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn

from core.attribution import attribute_graph
from core.complexity import compute_complexity
from core.cost_model import compute_cost
from core.graph import async_bfs
from core.output import render_json, render_terminal

console = Console(force_terminal=True)


@click.command()
@click.argument("target")
@click.option(
    "--depth", "-d",
    default=5,
    type=click.IntRange(1, 20),
    help="Max BFS hops to traverse (default: 5, max: 20).",
)
@click.option(
    "--node-limit", "-n",
    default=500,
    type=click.IntRange(10, 5000),
    help="Max transaction nodes to visit (default: 500).",
)
@click.option(
    "--direction",
    type=click.Choice(["forward", "backward", "both"], case_sensitive=False),
    default="forward",
    help="Traversal direction (default: forward).",
)
@click.option("--json", "output_json", is_flag=True, help="Output as JSON.")
@click.option("--verbose", "-v", is_flag=True, help="Show per-hop breakdown.")
@click.option("--methodology", is_flag=True, help="Show methodology and citations.")
@click.option(
    "--thorough",
    is_flag=True,
    help="Query all addresses via WalletExplorer (slower, more accurate).",
)
@click.option(
    "--no-walletexplorer",
    is_flag=True,
    help="Skip WalletExplorer queries (faster, local attribution only).",
)
@click.option(
    "--arkham-key",
    default=None,
    envvar="DUSTLINE_ARKHAM_KEY",
    help="Arkham Intelligence API key (enables Tier 3 attribution).",
)
@click.option(
    "--debug",
    is_flag=True,
    hidden=True,
    help="Enable debug logging.",
)
def main(
    target: str,
    depth: int,
    node_limit: int,
    direction: str,
    output_json: bool,
    verbose: bool,
    methodology: bool,
    thorough: bool,
    no_walletexplorer: bool,
    arkham_key: str,
    debug: bool,
) -> None:
    """Estimate the forensic cost of tracing a Bitcoin address or transaction.

    TARGET is a Bitcoin address (1..., 3..., bc1...) or transaction ID (64-char hex).
    """
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="%(name)s: %(message)s")

    try:
        asyncio.run(
            _run(
                target=target,
                depth=depth,
                node_limit=node_limit,
                direction=direction,
                output_json=output_json,
                verbose=verbose,
                methodology=methodology,
                thorough=thorough,
                no_walletexplorer=no_walletexplorer,
                arkham_key=arkham_key,
            )
        )
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        sys.exit(130)


async def _run(
    target: str,
    depth: int,
    node_limit: int,
    direction: str,
    output_json: bool,
    verbose: bool,
    methodology: bool,
    thorough: bool,
    no_walletexplorer: bool,
    arkham_key: str | None = None,
) -> None:
    """Async pipeline: BFS -> Attribution -> Complexity -> Cost -> Output."""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={"User-Agent": "DustLine/1.0 (Bitcoin forensic estimator)"},
        follow_redirects=True,
    ) as client:

        # Step 1: BFS graph construction
        if not output_json:
            console.print()
            console.print("[dim]Traversing transaction graph...[/dim]")

        graph = await async_bfs(
            client,
            target,
            max_depth=depth,
            node_limit=node_limit,
            direction=direction,
        )

        if not graph.root_txid:
            console.print(
                f"[bold red]Error:[/bold red] Could not resolve target: {target}"
            )
            console.print("[dim]Check that the address or txid is valid.[/dim]")
            sys.exit(1)

        if not output_json:
            console.print(
                f"  [dim]{len(graph.nodes)} nodes, "
                f"{len(graph.addresses_seen)} addresses[/dim]"
            )

        # Step 2: Attribution scan
        if not output_json:
            sources = ["local DB"]
            if not no_walletexplorer:
                sources.append("WalletExplorer")
            if arkham_key:
                sources.append("Arkham")
            console.print(f"[dim]Attributing addresses ({', '.join(sources)})...[/dim]")

        if thorough and not no_walletexplorer and not output_json:
            n_addrs = len(graph.addresses_seen)
            est_minutes = n_addrs / 0.8 / 60
            console.print(
                f"  [yellow]--thorough: ~{n_addrs} addresses to check, "
                f"est. ~{est_minutes:.0f} min at 0.8 req/s[/yellow]"
            )
            if est_minutes > 30:
                if not click.confirm(
                    f"  This will take ~{est_minutes:.0f} minutes. Continue?",
                    default=True,
                ):
                    console.print("[dim]Aborted. Run without --thorough for faster results.[/dim]")
                    sys.exit(0)

        graph = await attribute_graph(
            client,
            graph,
            skip_walletexplorer=no_walletexplorer,
            arkham_key=arkham_key,
            **({"we_limit": None} if thorough else {}),
        )

        # Step 3: Complexity scoring
        metrics = compute_complexity(graph)

        # Step 4-5: Cost estimation
        estimate = compute_cost(metrics)

        # Step 6: Output
        if output_json:
            render_json(graph, metrics, estimate)
        else:
            render_terminal(
                console, graph, metrics, estimate,
                verbose=verbose,
                methodology=methodology,
            )


if __name__ == "__main__":
    main()
