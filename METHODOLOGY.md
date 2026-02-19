# DustLine — Methodology

This document explains how DustLine estimates the cost of tracing a Bitcoin address. It covers the cost model, time estimates, data sources, multipliers, and known limitations. It is intended for researchers, forensic practitioners, and anyone who wants to understand or challenge the numbers the tool produces.

---

## Core premise

DustLine is not a tracing tool. It does not follow funds or identify entities. It estimates the analyst time and financial cost required to do that work manually — and flags the point where that cost becomes prohibitive.

The underlying argument is that Bitcoin privacy is often determined by economics, not cryptography. Heuristics like Common-Input-Ownership (CIOH) and change address detection don't need to fail completely to provide privacy. They just need to be expensive enough to apply that most adversaries give up. DustLine makes that threshold visible.

---

## Step 1: Graph traversal

DustLine builds a transaction graph starting from the target address using a breadth-first search (BFS):

1. Fetch the address's transaction history via mempool.space and Blockstream.info APIs
2. For each transaction, collect all input and output addresses
3. Follow outputs forward (or inputs backward, if `--direction backward`) to the next transaction
4. Continue until `--depth` hops or `--node-limit` nodes is reached

The result is a set of transaction nodes, each with an associated address list. This graph is the basis for all subsequent analysis.

**Why BFS?** BFS traverses the graph layer by layer, which maps naturally to "hops" in a forensic investigation. Each layer corresponds to one analyst decision point — which output to follow, which cluster to investigate next.

---

## Step 2: Attribution

Attribution determines how many addresses in the graph belong to known entities (exchanges, services, mining pools). This is the single most important input to the cost model — a well-attributed graph is fast to trace; an unattributed graph is slow and uncertain.

DustLine queries attribution in three tiers:

### Tier 1: Local database

A SQLite database built from two open-source datasets:

- **GraphSense TagPacks** ([github.com/graphsense/graphsense-tagpacks](https://github.com/graphsense/graphsense-tagpacks)) — curated YAML files containing Bitcoin addresses with verified entity labels. Covers exchanges, darknet markets, ransomware wallets, mixers, and other services. Maintained by a research consortium.
- **bitcoin-data/mining-pools** ([github.com/bitcoin-data/mining-pools](https://github.com/bitcoin-data/mining-pools)) — known coinbase payout addresses for major mining pools.

Combined, the local database contains approximately 483,000 labeled addresses. It is checked first on every address — no network request, no rate limit, instant.

### Tier 2: WalletExplorer

[WalletExplorer](https://www.walletexplorer.com) maintains approximately 315 named wallet clusters built using CIOH-based address clustering. It is queried for addresses unmatched by the local database.

**Important caveat:** WalletExplorer's label database was largely frozen around 2016 when its creator joined Chainalysis. It provides reasonable coverage for legacy P2PKH addresses associated with major services of that era. Coverage for modern bech32 (P2WPKH) and Taproot (P2TR) addresses is poor. DustLine queries WalletExplorer by default but skips anonymous cluster IDs — only named entities (e.g., "Bitstamp.net", "BTC-e.com") are recorded as attributions.

Rate limit: 0.8 requests/second. By default, DustLine samples up to 200 addresses. Use `--thorough` to query all addresses (slower, more accurate on large graphs).

### Tier 3: Arkham Intelligence (optional)

[Arkham Intelligence](https://www.arkhamintelligence.com) maintains 350M+ labeled addresses and 200K+ named entities, with substantially better coverage of modern bech32 and Taproot addresses. Requires a user-provided API key (`--arkham-key` or `DUSTLINE_ARKHAM_KEY` environment variable).

When configured, Arkham is queried for addresses unmatched by Tiers 1 and 2. Rate limit: 5 requests/second.

**Note:** Arkham API access requires approval. Apply at arkhamintelligence.com.

### Attribution rate

The overall attribution rate is: `attributed_addresses / total_addresses_in_graph`.

This rate drives the per-hop time estimate (see Step 3). It is reported transparently in every output alongside the coverage percentage — how much of the graph was actually checked. A 3% attribution rate means something very different at 100% coverage (truly unattributed) than at 2% coverage (database gaps are likely).

---

## Step 3: Per-hop time estimate

The per-hop base time is determined by attribution rate, based on practitioner estimates from TrailBit Labs operational data:

| Attribution rate | Base time per hop | Rationale |
|-----------------|------------------|-----------|
| > 70% | 12 minutes | Most addresses belong to known entities. Analyst can quickly confirm clusters and move to the next hop. |
| > 40% | 45 minutes | Enough known anchors to navigate, but significant manual verification required. |
| > 10% | 3 hours | Few anchors. Each hop requires manual OSINT, heuristic judgment, and cross-referencing. |
| ≤ 10% | 8 hours | Essentially unattributed. Each hop is an independent investigation from scratch. |

These figures represent careful manual analysis — not automated scanning. They include time for: applying CIOH and change detection heuristics, cross-referencing known entity databases, documenting methodology (important for legal contexts), and making judgment calls on ambiguous transactions.

**Empirical anchor:** A real TrailBit investigation involving 6,500 nodes required approximately 900 analyst-days and remained incomplete. This validates the upper end of the model — complex, unattributed graphs are not just expensive in theory.

**Published reference:** The 8 hrs/hop estimate for unattributed nodes is consistent with expert witness billing patterns in blockchain forensics cases, where practitioners routinely log 6–10 hours per transaction cluster in complex AML investigations.

---

## Step 4: Multipliers

Raw per-hop time is adjusted by multipliers that reflect real complexity drivers:

### Mixing multiplier (×3.5)

Applied when CoinJoin transactions are detected. CoinJoin breaks CIOH by design — multiple unrelated parties combine inputs, making input-to-output mapping probabilistic rather than deterministic. An analyst must: assess the specific CoinJoin implementation (Wasabi, JoinMarket, Whirlpool each have different detectability), attempt output correlation using amount analysis or timing, and consider whether to follow multiple plausible output paths.

The 3.5× figure reflects that a mixed transaction typically requires 3–4× the analyst time of an equivalent unmixed transaction, based on published case studies and practitioner estimates.

### Branch factor multiplier (linear above 5)

Applied when average outputs per transaction exceed 5. Formula: `branch_factor / 5`.

A transaction with 20 outputs creates 20 trace paths. Each path theoretically requires analysis. In practice, an analyst prioritizes based on output value, address type signals, and heuristic confidence — but even a triage pass at 20 outputs takes significantly longer than one at 2. The linear scaling is conservative; a more accurate model might use log-linear growth, but linear is defensible and easier to audit.

### Fan-in multiplier (uncapped, linear)

Applied to consolidation transactions (many inputs → one output). Formula: `input_count / 5`.

Fan-in affects backward tracing specifically. A 79-input consolidation means an investigator tracing backward must analyze 79 separate input chains — each potentially requiring full hop analysis. The cost estimate for `--direction forward` only includes forward traversal; backward fan-in multipliers are applied to consolidation nodes encountered during traversal to reflect the additional work required for complete attribution.

A 79-input consolidation applies ×15.8. This is not a rounding error — consolidations are genuinely expensive to trace completely.

### Taproot ratio multiplier (×1.4 above 50%)

Applied when more than 50% of addresses in the graph use Taproot (P2TR). Taproot eliminates several address type signals used in change detection:

- Address type matching (inputs and change typically share format) no longer distinguishes change from payment when both are P2TR
- Script path spends are indistinguishable from key path spends on-chain
- Multisig is visually identical to single-sig

When Taproot adoption in a graph is high, the analyst loses a significant heuristic tool. The 1.4× multiplier reflects estimated additional analyst time to reach equivalent confidence using remaining heuristics (value analysis, round number detection, spending timing).

---

## Step 5: Total cost calculation

```
hours_low  = base_hours_per_hop × depth × effective_multiplier
hours_high = hours_low × 1.6 + (unresolved_paths × 8.0)

cost = hours × (hourly_rate + tooling_overhead)
```

The `effective_multiplier` is the product of all applicable multipliers (mixing × branching × taproot × fan-in).

The 1.6× factor on the high estimate reflects real-world variance: complex judgments, documentation requirements, court preparation, and the non-linear cost of dead ends and backtracking. Unresolved paths — graph edges that could not be followed due to API failures or depth limits — each add 8 hours to the high estimate, representing a full unattributed hop of unknown complexity.

### Analyst rates

From the ExpertPages 2024 Expert Witness Survey (n=1,600+, median $451/hr for forensic specialists):

| Tier | Rate | Applies to |
|------|------|------------|
| Mid-level analyst | $200/hr | Standard compliance and AML work |
| Senior specialist | $450/hr + $150/hr tooling | Complex investigations, expert reports |
| Litigation expert | $1,000/hr + $150/hr tooling | Court-qualified expert witness testimony |

Tooling costs ($150/hr) reflect commercial blockchain analytics platform subscriptions (Chainalysis Reactor, Elliptic, TRM Labs) typically used by senior practitioners. Mid-level analysts often use open-source or lower-tier tooling not reflected in their base rate.

---

## Step 6: Confidence rating

Confidence reflects how reliable the cost estimate is, based on attribution coverage and whether any graph paths remain unresolved.

| Coverage | Unresolved paths | Confidence |
|----------|-----------------|-----------|
| ≥ 70% | 0 | High |
| ≥ 40% | Any | Moderate |
| ≥ 10% | Any | Low |
| < 10% | Any | Very low |

Special case: When all available attribution sources have been fully consulted (`sources_exhausted`), the confidence floor is raised to **moderate** even if the attribution rate is low. The rationale is that low attribution with exhaustive checking reflects genuinely unknown addresses — not incomplete data gathering. The estimate is as informed as possible given available sources.

---

## Pattern detection

DustLine classifies each transaction's structural pattern before applying multipliers. Pattern detection runs before attribution — CoinJoin detection specifically is prioritized because CoinJoin transactions often resemble consolidations in structure (many inputs, many equal outputs) and would be misclassified without explicit detection.

| Pattern | Detection criteria | Cost implication |
|---------|--------------------|-----------------|
| COINJOIN | Three heuristics (see below) — overrides structural classification | Mixing multiplier applied |
| CONSOLIDATION | ≥ 5 inputs, ≤ 2 outputs | Fan-in multiplier applied; forward-only note added |
| FAN-OUT | ≤ 3 inputs, ≥ 5 outputs | Branch multiplier applies to all output paths |
| PEEL CHAIN | ≤ 2 inputs, exactly 2 outputs | Sequential chain — branch factor near 1 |
| SIMPLE | All other cases | No pattern multiplier |

### CoinJoin detection heuristics

CoinJoin detection requires at least 5 total outputs and uses three independent checks:

1. **Known denomination match:** ≥ 3 outputs at a recognized Wasabi V1 (0.1 BTC) or Whirlpool denomination (0.001–0.5 BTC). Catches standard coordinator rounds.
2. **Generic equal-output:** ≥ 5 outputs at the same value, AND that group comprises > 50% of all outputs. The majority requirement prevents false positives from exchange batch payments (many outputs at different amounts).
3. **Multi-denomination groups (Wasabi v2):** ≥ 3 distinct value groups, each containing ≥ 3 equal outputs. Detects Wasabi v2's variable-denomination CoinJoin rounds.

All value comparisons use exact satoshi matching — no tolerance margin is applied.

RBF (Replace-by-Fee) signaling is detected via `sequence < 0xFFFFFFFE` on inputs per BIP 125. RBF is noted in output but does not currently affect cost estimates.

---

## Known gaps and future work

**No ground truth validation.** DustLine's time estimates have not been validated against a labeled dataset of real investigations with known analyst hours. The empirical anchor (900 analyst-days / 6,500 nodes) validates the extreme end; the mid-range is estimated. Contributions from forensic practitioners with real case data would improve accuracy.

**Commercial tooling not modeled.** Chainalysis Reactor and similar platforms use graph algorithms and proprietary data that can dramatically reduce per-hop time for well-attributed graphs. DustLine models manual analyst work. For highly attributed graphs (exchange-heavy flows), commercial tooling may reduce costs by 5–10×. For unattributed graphs, the gap is smaller — automation doesn't help when data doesn't exist.

**Attribution database staleness.** GraphSense TagPacks and WalletExplorer both have coverage gaps for addresses created after 2020. New exchange addresses, DeFi bridges, and institutional custodians are underrepresented. This biases DustLine toward overestimating cost for transactions involving modern services — a known conservative bias.

**No fee market modeling.** Transaction fees paid by the target address could inform the economic context (high fees suggest urgency or high-value transaction), but DustLine does not currently incorporate fee data into the cost model.

**Lightning Network.** DustLine analyzes on-chain transactions only. Funds that pass through Lightning channels are outside scope.

---

## Citing this tool

If you use DustLine in research, please cite:

```
Nicolaidis, G. (2026). DustLine: Economic Privacy Estimator for Bitcoin Transactions.
TrailBit Labs. https://github.com/TrailBit-Labs/DustLine
```

---

## References

- ExpertPages (2024). *Expert Witness Fee Survey*. n=1,600+. Median forensic specialist: $451/hr.
- Meiklejohn, S. et al. (2013). A fistful of bitcoins: Characterizing payments among men with no names. *IMC '13*.
- Nick, J. (2015). *Data-Driven De-Anonymization in Bitcoin*. ETH Zurich.
- Ergo / Möser, M. & Narayanan, A. (2017). An empirical analysis of traceability in the Monero blockchain.
- Kalodner, H. et al. (2020). BlockSci: Design and applications of a blockchain analysis platform. *USENIX Security*.
- Gong, Q. et al. (cited in Issue 1, Bitcoin Heuristics Field Notes). CIOH error rate: 63% in tested conditions.
- GraphSense TagPacks. [github.com/graphsense/graphsense-tagpacks](https://github.com/graphsense/graphsense-tagpacks)
- bitcoin-data/mining-pools. [github.com/bitcoin-data/mining-pools](https://github.com/bitcoin-data/mining-pools)

---

*Maintained by [TrailBit Labs](https://labs.trailbit.io). Questions and corrections welcome via GitHub Issues.*
