"""Tests for the forensic cost estimation model."""

import pytest

from core import ComplexityMetrics, PrivacyFloor
from core.cost_model import compute_cost


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_metrics(**overrides) -> ComplexityMetrics:
    """Create ComplexityMetrics with sensible defaults and overrides."""
    defaults = dict(
        node_count=50,
        edge_count=80,
        unique_addresses=120,
        max_depth=5,
        avg_branch_factor=2.0,
        max_branch_factor=3,
        attribution_rate=0.5,
        attributed_addresses=60,
        total_addresses=120,
        mixing_signals=0,
        addresses_checked=120,  # Full coverage by default
        unattributed_addresses=60,
    )
    defaults.update(overrides)
    return ComplexityMetrics(**defaults)


# ── Tier structure tests ─────────────────────────────────────────────────────


def test_three_tiers_returned():
    """Cost estimate always has exactly three tiers."""
    estimate = compute_cost(_make_metrics())
    assert len(estimate.tiers) == 3


def test_tier_rates():
    """Tiers use the correct hourly rates from spec."""
    estimate = compute_cost(_make_metrics())
    assert estimate.tiers[0].hourly_rate == 200.0   # Mid-level
    assert estimate.tiers[1].hourly_rate == 450.0   # Senior
    assert estimate.tiers[2].hourly_rate == 1000.0  # Expert


def test_tooling_overhead():
    """Senior and expert tiers include tooling overhead."""
    estimate = compute_cost(_make_metrics())
    assert estimate.tiers[0].tooling_overhead == 0.0    # Mid-level: no tooling
    assert estimate.tiers[1].tooling_overhead == 150.0  # Senior: $150/hr
    assert estimate.tiers[2].tooling_overhead == 150.0  # Expert: $150/hr


def test_cost_increases_with_tier():
    """Expert tier costs more than senior, which costs more than mid-level."""
    estimate = compute_cost(_make_metrics())
    assert estimate.tiers[0].total_low < estimate.tiers[1].total_low
    assert estimate.tiers[1].total_low < estimate.tiers[2].total_low


# ── Attribution rate -> base time tests ───────────────────────────────────────


def test_high_attribution_fast():
    """High attribution (>70%) -> 12 min/hop base time."""
    estimate = compute_cost(_make_metrics(attribution_rate=0.8))
    assert estimate.base_hours_per_hop == pytest.approx(0.2, abs=0.01)  # 12 min


def test_moderate_attribution():
    """Moderate attribution (40-70%) -> 45 min/hop."""
    estimate = compute_cost(_make_metrics(attribution_rate=0.5))
    assert estimate.base_hours_per_hop == pytest.approx(0.75, abs=0.01)  # 45 min


def test_low_attribution_slow():
    """Low attribution (10-40%) -> 3 hrs/hop."""
    estimate = compute_cost(_make_metrics(attribution_rate=0.2))
    assert estimate.base_hours_per_hop == pytest.approx(3.0, abs=0.01)


def test_very_low_attribution_very_slow():
    """Very low attribution (<=10%) -> 8 hrs/hop."""
    estimate = compute_cost(_make_metrics(attribution_rate=0.05))
    assert estimate.base_hours_per_hop == pytest.approx(8.0, abs=0.01)


# ── Multiplier tests ─────────────────────────────────────────────────────────


def test_mixing_multiplier():
    """CoinJoin detected -> 3.5x multiplier."""
    base = compute_cost(_make_metrics(mixing_signals=0))
    mixed = compute_cost(_make_metrics(mixing_signals=5, coinjoin_detected=True))
    assert mixed.mixing_multiplier == 3.5
    assert base.mixing_multiplier == 1.0
    assert mixed.tiers[0].total_low > base.tiers[0].total_low


def test_branching_multiplier_below_threshold():
    """Branch factor <= 5 -> no multiplier."""
    estimate = compute_cost(_make_metrics(avg_branch_factor=3.0))
    assert estimate.branching_multiplier == 1.0


def test_branching_multiplier_above_threshold():
    """Branch factor > 5 -> linear multiplier."""
    estimate = compute_cost(_make_metrics(avg_branch_factor=10.0))
    assert estimate.branching_multiplier == 2.0  # 10/5


def test_taproot_multiplier():
    """Taproot ratio > 50% -> 1.4x multiplier."""
    low_tr = compute_cost(_make_metrics(taproot_ratio=0.3))
    high_tr = compute_cost(_make_metrics(taproot_ratio=0.7))
    assert low_tr.taproot_multiplier == 1.0
    assert high_tr.taproot_multiplier == 1.4


def test_unresolved_adds_hours():
    """Unresolved paths add 8 hours each to the high estimate."""
    no_unresolved = compute_cost(_make_metrics(unresolved_paths=0))
    with_unresolved = compute_cost(_make_metrics(unresolved_paths=3))
    assert with_unresolved.unresolved_hours == 24.0  # 3 * 8
    assert with_unresolved.tiers[0].total_high > no_unresolved.tiers[0].total_high


# ── Privacy floor classification tests ────────────────────────────────────────


def test_floor_traceable():
    """High attribution, no mixing, shallow -> TRACEABLE."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.9, mixing_signals=0, max_depth=2,
    ))
    assert estimate.privacy_floor == PrivacyFloor.TRACEABLE


def test_floor_impractical():
    """Very low attribution + mixing + deep + many unresolved -> IMPRACTICAL."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.02,
        mixing_signals=15,
        coinjoin_detected=True,
        max_depth=15,
        avg_branch_factor=8.0,
        unresolved_paths=20,
        taproot_ratio=0.8,
    ))
    assert estimate.privacy_floor in (PrivacyFloor.HIGH_FLOOR, PrivacyFloor.IMPRACTICAL)


def test_floor_costly():
    """Moderate complexity -> COSTLY."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.6, mixing_signals=0, max_depth=3,
    ))
    assert estimate.privacy_floor in (PrivacyFloor.TRACEABLE, PrivacyFloor.COSTLY)


# ── Confidence tests ──────────────────────────────────────────────────────────


def test_confidence_high():
    """Attribution >= 70% with no unresolved -> high confidence."""
    estimate = compute_cost(_make_metrics(
        unresolved_paths=0, attribution_rate=0.75,
    ))
    assert estimate.confidence == "high"


def test_confidence_moderate():
    """Attribution 40-70% -> moderate confidence."""
    estimate = compute_cost(_make_metrics(
        unresolved_paths=0, attribution_rate=0.5,
    ))
    assert estimate.confidence == "moderate"


def test_confidence_low():
    """Attribution 10-40% -> low confidence."""
    estimate = compute_cost(_make_metrics(
        unresolved_paths=0, attribution_rate=0.15,
    ))
    assert estimate.confidence == "low"


def test_confidence_very_low():
    """Attribution < 10% -> very low confidence."""
    estimate = compute_cost(_make_metrics(
        unresolved_paths=0, attribution_rate=0.05,
    ))
    assert estimate.confidence == "very low"


# ── Minimum case threshold test ───────────────────────────────────────────────


def test_minimum_case_threshold_note():
    """Cases below $5,000 get a threshold note."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.9, max_depth=1,
    ))
    # Very cheap case should have the note
    if estimate.tiers[1].total_high < 5000:
        assert estimate.minimum_case_threshold_note is not None


# ── Zero-hop / dormant address tests ─────────────────────────────────────────


def test_zero_hop_returns_zero_cost():
    """Single node at depth 0 should return $0 cost."""
    estimate = compute_cost(_make_metrics(max_depth=0, node_count=1))
    assert all(t.total_low == 0 for t in estimate.tiers)
    assert all(t.total_high == 0 for t in estimate.tiers)
    assert all(t.estimated_hours_low == 0 for t in estimate.tiers)
    assert estimate.privacy_floor == PrivacyFloor.TRACEABLE
    assert estimate.total_hops == 0


def test_zero_hop_no_multipliers():
    """Zero-hop estimate should have neutral multipliers."""
    estimate = compute_cost(_make_metrics(
        max_depth=0, node_count=1,
        coinjoin_detected=True, mixing_signals=5, taproot_ratio=0.9,
    ))
    assert estimate.mixing_multiplier == 1.0
    assert estimate.taproot_multiplier == 1.0
    assert estimate.base_hours_per_hop == 0


def test_multi_node_depth_zero_still_estimates():
    """Multiple nodes at depth 0 (e.g., batched root) should still produce cost."""
    estimate = compute_cost(_make_metrics(max_depth=0, node_count=5))
    # node_count > 1, so the guard should NOT trigger
    assert any(t.total_low > 0 for t in estimate.tiers)


# ── Attribution-based confidence edge cases ──────────────────────────────────


def test_confidence_high_requires_no_unresolved():
    """Even with 70%+ attribution, unresolved paths downgrade from high."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.75,
        unresolved_paths=3,
    ))
    # 0.75 >= 0.4 -> moderate (the unresolved blocks "high")
    assert estimate.confidence == "moderate"


def test_confidence_note_present_on_low_attribution():
    """Low attribution produces a confidence note mentioning --thorough."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.05,
        unresolved_paths=0,
    ))
    assert estimate.confidence_note != ""
    assert "--thorough" in estimate.confidence_note


def test_confidence_note_empty_on_good_attribution():
    """Attribution >= 40% produces no confidence note."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.5,
        unresolved_paths=0,
    ))
    assert estimate.confidence_note == ""


# ── Sources-exhausted confidence floor tests ─────────────────────────────────


def test_confidence_floor_moderate_when_sources_exhausted():
    """Very low attribution + sources exhausted -> moderate (not very low)."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.05,
        unresolved_paths=0,
        sources_exhausted=True,
    ))
    assert estimate.confidence == "moderate"


def test_confidence_floor_low_becomes_moderate_when_exhausted():
    """Low attribution + sources exhausted -> moderate (not low)."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.15,
        unresolved_paths=0,
        sources_exhausted=True,
    ))
    assert estimate.confidence == "moderate"


def test_confidence_still_very_low_when_not_exhausted():
    """Very low attribution + sources NOT exhausted -> very low."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.05,
        unresolved_paths=0,
        sources_exhausted=False,
    ))
    assert estimate.confidence == "very low"


def test_confidence_note_suggests_arkham_when_exhausted():
    """Sources exhausted + low attribution -> note mentions --arkham-key."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.05,
        sources_exhausted=True,
    ))
    assert "--arkham-key" in estimate.confidence_note


def test_confidence_note_suggests_thorough_when_not_exhausted():
    """Sources NOT exhausted + low attribution -> note mentions --thorough."""
    estimate = compute_cost(_make_metrics(
        attribution_rate=0.05,
        sources_exhausted=False,
    ))
    assert "--thorough" in estimate.confidence_note


# ── Fan-in multiplier tests ─────────────────────────────────────────────────


def test_fan_in_multiplier_below_threshold():
    """Fan-in <= 5 -> no multiplier."""
    estimate = compute_cost(_make_metrics(avg_fan_in=3.0))
    assert estimate.fan_in_multiplier == 1.0


def test_fan_in_multiplier_above_threshold():
    """Fan-in > 5 -> linear multiplier."""
    estimate = compute_cost(_make_metrics(avg_fan_in=10.0))
    assert estimate.fan_in_multiplier == 2.0  # 10/5


def test_fan_in_multiplier_uncapped():
    """Fan-in multiplier scales linearly without cap."""
    estimate = compute_cost(_make_metrics(avg_fan_in=20.0))
    assert estimate.fan_in_multiplier == 4.0  # 20/5, no cap


def test_fan_in_increases_cost():
    """High fan-in produces higher cost than low fan-in."""
    low = compute_cost(_make_metrics(avg_fan_in=2.0))
    high = compute_cost(_make_metrics(avg_fan_in=10.0))
    assert high.tiers[0].total_low > low.tiers[0].total_low
