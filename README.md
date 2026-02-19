# DustLine

**Estimate the real-world cost of tracing a Bitcoin address.**

DustLine is an open-source tool that calculates the economic privacy floor for Bitcoin transactions — the point where forensic analysis becomes too expensive to justify. It doesn't tell you whether your transactions *can* be traced. It tells you what it would *cost*.

Built by [TrailBit Labs](https://labs.trailbit.io). Companion tool to [Bitcoin Heuristics Field Notes](https://geonicolaidis.substack.com/): *Privacy Through Economics, Not Cryptography*.

---

## Why this exists

Most conversations about Bitcoin privacy focus on whether a transaction is *technically* traceable. That's the wrong question. Almost every Bitcoin transaction is technically traceable. The real question is whether anyone will spend the money to trace it.

Forensic analysts charge $200–$1,000+/hr. Major investigations require hundreds of analyst-days. Simple traces through unattributed nodes take hours per hop. At some point, the cost of analysis exceeds the value of the information — and that's where practical privacy begins.

DustLine makes this concrete.

---

## Installation

```bash
git clone https://github.com/TrailBit-Labs/DustLine
cd DustLine
pip install -r requirements.txt

# Build the attribution database (~483K labeled addresses)
python data/build_db.py --download
```

The `--download` flag clones [GraphSense TagPacks](https://github.com/graphsense/graphsense-tagpacks) and [bitcoin-data/mining-pools](https://github.com/bitcoin-data/mining-pools) into a temp directory, builds the SQLite database, and cleans up. Takes 2–3 minutes. Requires `git` on PATH.

DustLine works without the database (attribution rate will be 0%), but cost estimates will be significantly overstated.

**Requirements:** Python 3.8+, git, internet access for blockchain and attribution APIs.

---

## Usage

```bash
# Basic estimate
python dustline.py <bitcoin_address>

# Verbose output with per-hop breakdown
python dustline.py <bitcoin_address> --verbose

# Trace deeper (default: 5 hops, max: 20)
python dustline.py <bitcoin_address> --depth 10

# Limit nodes visited (default: 500, max: 5000)
python dustline.py <bitcoin_address> --node-limit 1000

# Trace backward (inputs) or both directions
python dustline.py <bitcoin_address> --direction backward

# Check all addresses, not just a sample
python dustline.py <bitcoin_address> --thorough

# Skip WalletExplorer queries (faster, local attribution only)
python dustline.py <bitcoin_address> --no-walletexplorer

# Include Arkham Intelligence for better bech32/taproot coverage
python dustline.py <bitcoin_address> --arkham-key YOUR_API_KEY

# Output as JSON
python dustline.py <bitcoin_address> --json

# Show methodology and citations
python dustline.py <bitcoin_address> --methodology
```

### CLI flags

| Flag | Short | Default | Description |
|------|-------|---------|-------------|
| `--depth` | `-d` | 5 | Max BFS hops to traverse (1–20) |
| `--node-limit` | `-n` | 500 | Max transaction nodes to visit (10–5000) |
| `--direction` | | forward | Traversal direction: `forward`, `backward`, or `both` |
| `--verbose` | `-v` | | Show per-hop breakdown |
| `--json` | | | Output as JSON |
| `--methodology` | | | Show methodology and citations |
| `--thorough` | | | Query all addresses via WalletExplorer (slower, more accurate) |
| `--no-walletexplorer` | | | Skip WalletExplorer queries (faster, local attribution only) |
| `--arkham-key` | | | Arkham Intelligence API key (or set `DUSTLINE_ARKHAM_KEY` env var) |

---

## Example output

```
DustLine v1.0 — Economic Privacy Estimator
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Target:          bc1qm34lsc65zpw79lxes69zkqmk6ee3ewf0j77s3h

GRAPH COMPLEXITY
  Branch factor:      18.0 (high fragmentation)
  Attribution rate:   3% (1/35 addresses)
  Mixing detected:    No

COST ESTIMATE
  Mid-level analyst ($200/hr)            $5,760 – $9,216
  Senior specialist ($450+$150/hr)     $17,280 – $27,648
  Litigation expert ($1000+$150/hr)    $33,120 – $52,992

ECONOMIC PRIVACY FLOOR
  🟠 EXPENSIVE
  Requires significant financial motivation. Out of reach for most private actors.
```

### Privacy floor ratings

| Rating | Senior analyst cost | What it means |
|--------|-------------------|---------------|
| 🔴 Traceable | < $500 | Trivial to trace. No meaningful barrier. |
| 🟡 Costly | $500 – $5,000 | Requires motivation. Beyond casual investigation. |
| 🟠 Expensive | $5,000 – $50,000 | Significant financial or legal stakes needed. |
| 🟢 High Floor | $50,000 – $500,000 | Only serious investigations (law enforcement, major litigation). |
| 🟣 Impractical | > $500,000 | Beyond the budget of most adversaries. |

---

## How it works

### Cost model

Time-per-hop is driven by attribution rate — how many addresses in the graph belong to known entities:

| Attribution rate | Time per hop | Interpretation |
|-----------------|-------------|----------------|
| > 70% | 12 min | Most nodes known — fast traversal |
| > 40% | 45 min | Some anchors to work from |
| > 10% | 3 hrs | Mostly unknown — slow, uncertain |
| ≤ 10% | 8 hrs | Essentially unattributed |

**Multipliers applied:**
- Mixing detected (CoinJoin, etc.): ×3.5
- High branch factor: scales linearly above 5 branches
- Fan-in (consolidation): scales with input count — a 79-input consolidation applies ×15.8
- Taproot ratio > 50%: ×1.4 (address type heuristics degraded)

**Analyst rates** from ExpertPages 2024 Expert Witness Survey (n=1,600+):
- Mid-level: $200/hr
- Senior specialist: $450/hr + $150/hr tooling
- Litigation expert: $1,000/hr + $150/hr tooling

### Attribution sources

DustLine queries attribution in tiers:

1. **Local database** — GraphSense TagPacks + mining pool addresses (~483K labeled addresses). Instant, offline, no rate limits. Checked first on every address.
2. **WalletExplorer** — ~315 named wallet clusters. Queried for addresses unmatched by local DB. Best for legacy addresses and known services.
3. **Arkham Intelligence** *(optional, requires API key)* — 350M+ addresses, 200K+ entities. Best coverage for modern bech32/taproot addresses. Pass `--arkham-key` or set `DUSTLINE_ARKHAM_KEY` environment variable.

Attribution coverage is reported transparently in every output. Confidence ratings reflect how much of the graph was actually checked, not just whether the tool ran successfully.

### Pattern detection

DustLine classifies transaction structure before applying cost multipliers:

- **SIMPLE** — standard 1-in/2-out transaction
- **PEEL CHAIN** — sequential single-output spending
- **FAN-OUT** — batch payment or distribution (each output is a separate trace path)
- **CONSOLIDATION** — many inputs to one output (note: cost estimate covers forward tracing only; backward tracing all inputs may be significantly more expensive)
- **COINJOIN** — equal-value outputs indicating mixing; detected first, overrides structural classification

---

## Limitations

**Read this before drawing conclusions from DustLine output.**

**DustLine estimates cost, not traceability.** A 🟣 IMPRACTICAL rating does not mean your transactions cannot be traced. It means tracing them would be expensive using the analyst rates and time model in this tool. A well-funded adversary with proprietary data may reach different conclusions.

**Attribution coverage is incomplete.** No public dataset labels more than a fraction of Bitcoin addresses. Modern bech32 and Taproot addresses are particularly underrepresented in free datasets. Low attribution rates increase estimated cost — but they may also reflect gaps in the database rather than genuinely unattributed addresses. Always check the coverage percentage in the output.

**The cost model is a practitioner estimate, not a certified standard.** Time-per-hop figures are based on TrailBit Labs operational data and published forensic investigation rates. Actual costs vary with analyst skill, available tooling, and case context. The model has not been independently validated.

**Error rates compound across hops.** Each hop in the trace builds on assumptions from the previous one. Over 5+ hops through unattributed nodes, confidence degrades significantly. DustLine tracks this but cannot fully quantify compounding error.

**Automated tools may reduce costs.** DustLine models manual analyst time. Commercial forensic platforms (Chainalysis, Elliptic, TRM Labs) use automation that may reduce per-hop costs for straightforward traces. Highly fragmented or mixed transactions remain expensive even for automated tools.

**This is not legal advice.** DustLine does not assess compliance risk, legal exposure, or regulatory obligations. If you have legal questions about Bitcoin transactions, consult a qualified attorney.

---

## Data sources

- **GraphSense TagPacks** — [github.com/graphsense/graphsense-tagpacks](https://github.com/graphsense/graphsense-tagpacks)
- **bitcoin-data/mining-pools** — [github.com/bitcoin-data/mining-pools](https://github.com/bitcoin-data/mining-pools)
- **WalletExplorer** — [walletexplorer.com](https://www.walletexplorer.com) (API)
- **Arkham Intelligence** — [arkhamintelligence.com](https://www.arkhamintelligence.com) (optional, requires API key)
- **Blockchain data** — mempool.space, Blockstream.info

---

## Research context

DustLine is a companion tool to *Bitcoin Heuristics Field Notes*, a newsletter examining forensic methodologies for tracing Bitcoin transactions.

The core thesis: Bitcoin privacy often comes not from cryptographic techniques but from economic constraints that make forensic analysis cost-prohibitive. Simple fragmentation can create an "economic privacy floor" where low-value flows become practically private through cost-benefit mathematics — not because the heuristics fail, but because applying them comprehensively exceeds the available budget.

More at [labs.trailbit.io](https://labs.trailbit.io).

---

## License

MIT. Use freely. Attribution appreciated.

---

*Built by [Geo Nicolaidis](https://geonicolaidis.com) / [TrailBit Labs](https://labs.trailbit.io)*
