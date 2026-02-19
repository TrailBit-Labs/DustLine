"""DustLine core data models.

All dataclasses and enums live here to prevent circular imports.
Every other module in core/ imports from this file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ── Enums ─────────────────────────────────────────────────────────────────────


class ScriptType(Enum):
    """Bitcoin output script types."""

    P2PKH = "p2pkh"  # Legacy (1...)
    P2SH = "p2sh"  # Script hash, includes wrapped segwit (3...)
    P2WPKH = "p2wpkh"  # Native segwit v0 (bc1q...)
    P2WSH = "p2wsh"  # Segwit script hash
    P2TR = "p2tr"  # Taproot (bc1p...)
    UNKNOWN = "unknown"

    @classmethod
    def from_esplora(cls, scriptpubkey_type: str) -> ScriptType:
        """Map Esplora API scriptpubkey_type strings to ScriptType."""
        mapping = {
            "p2pkh": cls.P2PKH,
            "p2sh": cls.P2SH,
            "v0_p2wpkh": cls.P2WPKH,
            "v0_p2wsh": cls.P2WSH,
            "v1_p2tr": cls.P2TR,
        }
        return mapping.get(scriptpubkey_type, cls.UNKNOWN)


class PrivacyFloor(Enum):
    """Five-level classification of tracing economic viability."""

    TRACEABLE = "traceable"  # < $500
    COSTLY = "costly"  # $500 – $5,000
    EXPENSIVE = "expensive"  # $5,000 – $50,000
    HIGH_FLOOR = "high_floor"  # $50,000 – $500,000
    IMPRACTICAL = "impractical"  # > $500,000

    @property
    def emoji(self) -> str:
        return {
            PrivacyFloor.TRACEABLE: "\U0001f534",
            PrivacyFloor.COSTLY: "\U0001f7e1",
            PrivacyFloor.EXPENSIVE: "\U0001f7e0",
            PrivacyFloor.HIGH_FLOOR: "\U0001f7e2",
            PrivacyFloor.IMPRACTICAL: "\U0001f7e3",
        }[self]

    @property
    def label(self) -> str:
        return {
            PrivacyFloor.TRACEABLE: "TRACEABLE",
            PrivacyFloor.COSTLY: "COSTLY",
            PrivacyFloor.EXPENSIVE: "EXPENSIVE",
            PrivacyFloor.HIGH_FLOOR: "HIGH FLOOR",
            PrivacyFloor.IMPRACTICAL: "IMPRACTICAL",
        }[self]


class TxPattern(Enum):
    """Common Bitcoin transaction patterns."""

    CONSOLIDATION = "consolidation"  # Many inputs -> few outputs
    PEEL_CHAIN = "peel_chain"  # 1-2 inputs -> 2 outputs (payment + change)
    FAN_OUT = "fan_out"  # Few inputs -> many outputs (batch payment)
    COINJOIN = "coinjoin"  # Many equal-value outputs (detected separately)
    SIMPLE = "simple"  # Doesn't match a specific pattern

    @property
    def label(self) -> str:
        return {
            TxPattern.CONSOLIDATION: "CONSOLIDATION",
            TxPattern.PEEL_CHAIN: "PEEL CHAIN",
            TxPattern.FAN_OUT: "FAN-OUT",
            TxPattern.COINJOIN: "COINJOIN",
            TxPattern.SIMPLE: "SIMPLE",
        }[self]


# ── Transaction primitives ────────────────────────────────────────────────────


@dataclass
class TxInput:
    """A single transaction input."""

    prev_txid: str
    prev_vout: int
    address: Optional[str]  # None for coinbase inputs
    value_sat: int
    script_type: ScriptType


@dataclass
class TxOutput:
    """A single transaction output."""

    address: Optional[str]  # None for OP_RETURN or unparseable
    value_sat: int
    script_type: ScriptType
    spent: bool = False
    spending_txid: Optional[str] = None


# ── Graph structures ──────────────────────────────────────────────────────────


@dataclass
class GraphNode:
    """A node in the BFS graph. Each node is a transaction.

    Transactions are the atomic unit in Bitcoin's UTXO model.
    Addresses appear as fields within TxInput/TxOutput.
    """

    txid: str
    inputs: list[TxInput] = field(default_factory=list)
    outputs: list[TxOutput] = field(default_factory=list)
    fee_sat: int = 0
    size_bytes: int = 0
    weight: int = 0
    timestamp: Optional[int] = None  # Unix epoch
    block_height: Optional[int] = None
    depth: int = 0  # BFS depth from root
    is_coinbase: bool = False
    rbf_signaled: bool = False  # True if any input has sequence < 0xFFFFFFFE
    resolved: bool = True  # False if API call failed
    # Maps address -> entity name, e.g. {"1A1z...": "Binance"}
    attributed_entities: dict[str, str] = field(default_factory=dict)


@dataclass
class GraphEdge:
    """Directed edge linking two transactions via a spent output."""

    from_txid: str
    to_txid: str
    address: Optional[str]
    value_sat: int
    vout_index: int


@dataclass
class AttributionResult:
    """Attribution result for a single address from any source."""

    address: str
    entity: str
    source: str  # "local_db", "walletexplorer", "arkham"
    category: str = ""  # "exchange", "mining_pool", "service", "notable"
    confidence: str = ""  # "confirmed", "probable", "cluster"


@dataclass
class AttributionSummary:
    """Aggregate attribution statistics across all sources."""

    total_addresses: int = 0
    attributed_count: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    by_category: dict[str, int] = field(default_factory=dict)
    coverage_rate: float = 0.0
    sources_used: list[str] = field(default_factory=list)


@dataclass
class GraphResult:
    """Complete result of BFS traversal."""

    root_input: str  # Original user input (txid or address)
    root_txid: str  # Starting transaction ID
    nodes: dict[str, GraphNode] = field(default_factory=dict)
    edges: list[GraphEdge] = field(default_factory=list)
    addresses_seen: set[str] = field(default_factory=set)
    max_depth_reached: int = 0
    requested_max_depth: int = 0  # The --depth value the user asked for
    node_limit_hit: bool = False
    unresolved_count: int = 0
    is_dormant: bool = False  # True if target address has never spent
    dormancy_note: str = ""  # Human-readable explanation when dormant
    api_provider_used: str = "mempool"  # Primary provider that served most data
    we_addresses_queried: int = 0  # Addresses actually sent to WalletExplorer
    we_addresses_total_unmatched: int = 0  # Addresses unmatched after local DB
    warnings: list[str] = field(default_factory=list)
    attribution_results: list[AttributionResult] = field(default_factory=list)
    attribution_summary: Optional[AttributionSummary] = None


# ── Analysis results ──────────────────────────────────────────────────────────


@dataclass
class ComplexityMetrics:
    """Computed complexity metrics for the traversed graph."""

    node_count: int
    edge_count: int
    unique_addresses: int
    max_depth: int
    avg_branch_factor: float
    max_branch_factor: int
    attribution_rate: float  # 0.0 to 1.0
    attributed_addresses: int
    total_addresses: int
    mixing_signals: int  # Count of suspected CoinJoin transactions
    mixing_txids: list[str] = field(default_factory=list)
    coinjoin_detected: bool = False
    taproot_ratio: float = 0.0
    unresolved_paths: int = 0
    addresses_checked: int = 0  # Addresses checked against any source (local DB + WE)
    unattributed_addresses: int = 0  # Addresses with no entity label
    sources_exhausted: bool = False  # True when all available sources were fully consulted
    avg_fan_in: float = 1.0  # Average inputs per transaction
    max_fan_in: int = 1  # Maximum inputs in any single transaction
    root_pattern: Optional[TxPattern] = None  # Detected pattern of root tx
    root_pattern_detail: str = ""  # e.g., "79-in -> 1-out"
    script_type_counts: dict[str, int] = field(default_factory=dict)
    total_value_sat: int = 0


@dataclass
class TierEstimate:
    """Cost estimate for a single analyst tier."""

    tier_name: str
    hourly_rate: float  # USD
    tooling_overhead: float  # USD/hr
    estimated_hours_low: float
    estimated_hours_high: float
    total_low: float  # USD
    total_high: float  # USD


@dataclass
class CostEstimate:
    """Complete cost estimation result."""

    tiers: list[TierEstimate]  # Mid-level, Senior, Expert
    base_hours_per_hop: float
    total_hops: int
    mixing_multiplier: float
    branching_multiplier: float
    taproot_multiplier: float
    fan_in_multiplier: float
    unresolved_hours: float
    privacy_floor: PrivacyFloor
    privacy_floor_summary: str
    confidence: str  # "high", "moderate", "low", "very low"
    confidence_note: str = ""  # Human-readable explanation of confidence rating
    minimum_case_threshold_note: Optional[str] = None
