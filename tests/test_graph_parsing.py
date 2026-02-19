"""Tests for transaction parsing and graph construction helpers."""

import json
from pathlib import Path

import pytest

from core import ScriptType
from core.graph import _parse_tx, _get_neighbors, _build_edges, GraphNode, GraphResult

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


# ── Transaction parsing tests ─────────────────────────────────────────────────


def test_parse_simple_tx():
    """Parse a standard coinbase transaction."""
    tx_data = _load_fixture("mempool_tx_simple.json")
    node = _parse_tx(tx_data, depth=0)

    assert node.txid == "a1075db55d416d3ca199f55b6084e2115b9345e16c5cf302fc80e9d5fbf5d48d"
    assert len(node.outputs) == 1
    assert node.outputs[0].address == "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
    assert node.outputs[0].value_sat == 5_000_000_000
    assert node.outputs[0].script_type == ScriptType.P2PKH
    assert node.is_coinbase is True
    assert node.block_height == 0
    assert node.depth == 0
    assert node.resolved is True


def test_parse_coinjoin_tx():
    """Parse a CoinJoin-style transaction with many outputs."""
    tx_data = _load_fixture("mempool_tx_coinjoin.json")
    node = _parse_tx(tx_data, depth=1)

    assert len(node.inputs) == 5
    assert len(node.outputs) == 10
    assert node.depth == 1
    assert node.is_coinbase is False
    # All inputs should have v0_p2wpkh script type
    for inp in node.inputs:
        assert inp.script_type == ScriptType.P2WPKH


def test_parse_with_outspends():
    """Outspend data is correctly applied to outputs."""
    tx_data = _load_fixture("mempool_tx_simple.json")
    outspends = _load_fixture("mempool_outspends.json")
    node = _parse_tx(tx_data, depth=0, outspends=outspends)

    assert node.outputs[0].spent is True
    assert node.outputs[0].spending_txid == "next_tx_1"


def test_parse_without_outspends():
    """Without outspend data, outputs default to unspent."""
    tx_data = _load_fixture("mempool_tx_simple.json")
    node = _parse_tx(tx_data, depth=0, outspends=None)

    assert node.outputs[0].spent is False
    assert node.outputs[0].spending_txid is None


# ── ScriptType mapping tests ─────────────────────────────────────────────────


def test_script_type_mapping():
    """Esplora script types map correctly."""
    assert ScriptType.from_esplora("p2pkh") == ScriptType.P2PKH
    assert ScriptType.from_esplora("p2sh") == ScriptType.P2SH
    assert ScriptType.from_esplora("v0_p2wpkh") == ScriptType.P2WPKH
    assert ScriptType.from_esplora("v0_p2wsh") == ScriptType.P2WSH
    assert ScriptType.from_esplora("v1_p2tr") == ScriptType.P2TR
    assert ScriptType.from_esplora("something_new") == ScriptType.UNKNOWN


# ── Neighbor extraction tests ─────────────────────────────────────────────────


def test_get_neighbors_forward():
    """Forward direction follows spent outputs."""
    from core import TxOutput, TxInput
    node = GraphNode(
        txid="tx1",
        inputs=[TxInput("prev", 0, "addr", 100, ScriptType.P2WPKH)],
        outputs=[
            TxOutput("out1", 50, ScriptType.P2WPKH, spent=True, spending_txid="tx2"),
            TxOutput("out2", 50, ScriptType.P2WPKH, spent=False),
        ],
    )
    neighbors = _get_neighbors(node, "forward")
    assert neighbors == ["tx2"]


def test_get_neighbors_backward():
    """Backward direction follows input sources."""
    from core import TxOutput, TxInput
    node = GraphNode(
        txid="tx2",
        inputs=[
            TxInput("tx1", 0, "addr1", 100, ScriptType.P2WPKH),
            TxInput("tx0", 1, "addr2", 50, ScriptType.P2WPKH),
        ],
        outputs=[TxOutput("out1", 140, ScriptType.P2WPKH)],
    )
    neighbors = _get_neighbors(node, "backward")
    assert set(neighbors) == {"tx1", "tx0"}


def test_get_neighbors_both():
    """Both direction combines forward and backward."""
    from core import TxOutput, TxInput
    node = GraphNode(
        txid="tx_mid",
        inputs=[TxInput("tx_prev", 0, "addr1", 100, ScriptType.P2WPKH)],
        outputs=[
            TxOutput("out1", 90, ScriptType.P2WPKH, spent=True, spending_txid="tx_next"),
        ],
    )
    neighbors = _get_neighbors(node, "both")
    assert set(neighbors) == {"tx_prev", "tx_next"}


def test_get_neighbors_coinbase_backward():
    """Coinbase transactions have no backward neighbors."""
    from core import TxOutput, TxInput
    node = GraphNode(
        txid="coinbase_tx",
        inputs=[TxInput("0" * 64, 0xFFFFFFFF, None, 0, ScriptType.UNKNOWN)],
        outputs=[TxOutput("miner", 625_000_000, ScriptType.P2WPKH)],
        is_coinbase=True,
    )
    neighbors = _get_neighbors(node, "backward")
    assert neighbors == []
