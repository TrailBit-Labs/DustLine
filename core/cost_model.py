"""Forensic cost estimation model.

Translates ComplexityMetrics into analyst time and dollar cost estimates
across three tiers, then classifies the economic privacy floor.

All rates and thresholds sourced from:
- ExpertPages 2024 Expert Witness Fees Survey (median $451/hr, n=1,600+)
- SEAK 2024 Expert Witness Survey (median file review $450/hr)
- TrailBit Labs practitioner estimates (2023-2024)
- A&D Forensics minimum case threshold ($5,000)
"""

from core import (
    ComplexityMetrics,
    CostEstimate,
    PrivacyFloor,
    TierEstimate,
)

# ── Analyst tiers ─────────────────────────────────────────────────────────────

TIERS = [
    {"name": "Mid-level analyst", "rate": 200.0, "tooling": 0.0},
    {"name": "Senior specialist", "rate": 450.0, "tooling": 150.0},
    {"name": "Litigation expert", "rate": 1000.0, "tooling": 150.0},
]

# ── Time estimation constants ─────────────────────────────────────────────────

# Base minutes per hop, keyed by attribution rate thresholds (descending)
BASE_TIME_THRESHOLDS = [
    (0.7, 12),   # >70% attributed: 12 min/hop
    (0.4, 45),   # >40%: 45 min/hop
    (0.1, 180),  # >10%: 3 hrs/hop
    (0.0, 480),  # <=10%: 8 hrs/hop
]

MIXING_MULTIPLIER = 3.5
TAPROOT_THRESHOLD = 0.5
TAPROOT_MULTIPLIER = 1.4
UNRESOLVED_HOURS_EACH = 8.0

# Confidence interval: low estimate uses base, high uses base * this factor
HIGH_ESTIMATE_FACTOR = 1.6

# Privacy floor dollar thresholds (based on senior specialist tier)
FLOOR_THRESHOLDS = [
    (500, PrivacyFloor.TRACEABLE),
    (5_000, PrivacyFloor.COSTLY),
    (50_000, PrivacyFloor.EXPENSIVE),
    (500_000, PrivacyFloor.HIGH_FLOOR),
]

MINIMUM_CASE_THRESHOLD = 5_000  # A&D Forensics confirmed


def compute_cost(metrics: ComplexityMetrics) -> CostEstimate:
    """Estimate forensic tracing cost from graph complexity metrics."""

    # Guard: dormant or empty graph — no tracing required
    if metrics.max_depth == 0 and metrics.node_count <= 1:
        zero_tiers = [
            TierEstimate(
                tier_name=t["name"],
                hourly_rate=t["rate"],
                tooling_overhead=t["tooling"],
                estimated_hours_low=0,
                estimated_hours_high=0,
                total_low=0,
                total_high=0,
            )
            for t in TIERS
        ]
        return CostEstimate(
            tiers=zero_tiers,
            base_hours_per_hop=0,
            total_hops=0,
            mixing_multiplier=1.0,
            branching_multiplier=1.0,
            taproot_multiplier=1.0,
            fan_in_multiplier=1.0,
            unresolved_hours=0,
            privacy_floor=PrivacyFloor.TRACEABLE,
            privacy_floor_summary=(
                "No tracing required \u2014 single node with no outgoing activity."
            ),
            confidence="high",
        )

    # Base time per hop
    base_minutes = _base_time_per_hop(metrics.attribution_rate)
    base_hours_per_hop = base_minutes / 60.0

    # Apply multipliers
    mixing_mult = MIXING_MULTIPLIER if metrics.coinjoin_detected else 1.0

    branching_mult = 1.0
    if metrics.avg_branch_factor > 5:
        branching_mult = metrics.avg_branch_factor / 5.0

    taproot_mult = 1.0
    if metrics.taproot_ratio > TAPROOT_THRESHOLD:
        taproot_mult = TAPROOT_MULTIPLIER

    fan_in_mult = 1.0
    if metrics.avg_fan_in > 5:
        fan_in_mult = metrics.avg_fan_in / 5.0  # Uncapped — 79 inputs is 15.8x more work than 5

    effective_mult = mixing_mult * branching_mult * taproot_mult * fan_in_mult

    # Total hours
    total_hops = max(metrics.max_depth, 1)
    base_total = base_hours_per_hop * total_hops * effective_mult
    unresolved_hours = metrics.unresolved_paths * UNRESOLVED_HOURS_EACH

    hours_low = base_total
    hours_high = base_total * HIGH_ESTIMATE_FACTOR + unresolved_hours

    # Per-tier cost estimates
    tiers = []
    for tier_def in TIERS:
        effective_rate = tier_def["rate"] + tier_def["tooling"]
        tiers.append(
            TierEstimate(
                tier_name=tier_def["name"],
                hourly_rate=tier_def["rate"],
                tooling_overhead=tier_def["tooling"],
                estimated_hours_low=round(hours_low, 1),
                estimated_hours_high=round(hours_high, 1),
                total_low=round(hours_low * effective_rate, 0),
                total_high=round(hours_high * effective_rate, 0),
            )
        )

    # Privacy floor classification (uses senior specialist tier)
    senior = tiers[1]
    reference_cost = (senior.total_low + senior.total_high) / 2
    privacy_floor = _classify_floor(reference_cost)
    summary = _floor_summary(privacy_floor, senior)

    # Confidence based on attribution rate (what fraction got a label)
    coverage_rate = metrics.attribution_rate

    if coverage_rate >= 0.7 and metrics.unresolved_paths == 0:
        confidence = "high"
    elif coverage_rate >= 0.4:
        confidence = "moderate"
    elif coverage_rate >= 0.1:
        confidence = "low"
    else:
        confidence = "very low"

    # When all available sources were fully consulted, low attribution
    # reflects genuinely unknown addresses — not incomplete checking.
    # Floor confidence at "moderate" since the estimate is as informed as possible.
    if metrics.sources_exhausted and confidence in ("low", "very low"):
        confidence = "moderate"

    # Confidence note when attribution is poor
    confidence_note = ""
    if coverage_rate < 0.4 and metrics.total_addresses > 0:
        attributed = metrics.attributed_addresses
        total = metrics.total_addresses
        if metrics.sources_exhausted:
            confidence_note = (
                f"Only {coverage_rate:.0%} of addresses attributed "
                f"({attributed}/{total}). "
                f"Unattributed addresses may include unlabeled exchange "
                f"or service nodes. "
                f"Add --arkham-key for better bech32/taproot coverage."
            )
        else:
            confidence_note = (
                f"Only {coverage_rate:.0%} of addresses attributed "
                f"({attributed}/{total}). "
                f"Cost estimate may be significantly overstated if unattributed "
                f"addresses include exchange or service nodes. "
                f"Run with --thorough to check all addresses."
            )

    # Minimum case threshold note
    threshold_note = None
    if senior.total_high < MINIMUM_CASE_THRESHOLD:
        threshold_note = (
            f"Most forensic firms require a minimum ${MINIMUM_CASE_THRESHOLD:,} "
            f"investigation value (A&D Forensics, confirmed public)."
        )

    return CostEstimate(
        tiers=tiers,
        base_hours_per_hop=base_hours_per_hop,
        total_hops=total_hops,
        mixing_multiplier=mixing_mult,
        branching_multiplier=round(branching_mult, 2),
        taproot_multiplier=taproot_mult,
        fan_in_multiplier=round(fan_in_mult, 2),
        unresolved_hours=unresolved_hours,
        privacy_floor=privacy_floor,
        privacy_floor_summary=summary,
        confidence=confidence,
        confidence_note=confidence_note,
        minimum_case_threshold_note=threshold_note,
    )


def _base_time_per_hop(attribution_rate: float) -> float:
    """Return base minutes per hop given the attribution rate."""
    for threshold, minutes in BASE_TIME_THRESHOLDS:
        if attribution_rate > threshold:
            return float(minutes)
    return float(BASE_TIME_THRESHOLDS[-1][1])


def _classify_floor(reference_cost_usd: float) -> PrivacyFloor:
    """Classify privacy floor from the reference (senior tier) cost."""
    for threshold, floor in FLOOR_THRESHOLDS:
        if reference_cost_usd < threshold:
            return floor
    return PrivacyFloor.IMPRACTICAL


def _floor_summary(floor: PrivacyFloor, senior: TierEstimate) -> str:
    """Human-readable privacy floor summary."""
    cost_range = f"${senior.total_low:,.0f}\u2013${senior.total_high:,.0f}"
    summaries = {
        PrivacyFloor.TRACEABLE: (
            f"{cost_range} for senior analyst. "
            "Any motivated party can afford this trace."
        ),
        PrivacyFloor.COSTLY: (
            f"{cost_range} for senior analyst. "
            "Viable for law enforcement, out of reach for most private actors."
        ),
        PrivacyFloor.EXPENSIVE: (
            f"{cost_range} for senior analyst. "
            "Requires significant financial motivation. "
            "Out of reach for most private actors."
        ),
        PrivacyFloor.HIGH_FLOOR: (
            f"{cost_range} for senior analyst. "
            "Only justified by very large amounts at stake."
        ),
        PrivacyFloor.IMPRACTICAL: (
            f"{cost_range} for senior analyst. "
            "Economically invisible to all but nation-state actors."
        ),
    }
    return summaries[floor]
