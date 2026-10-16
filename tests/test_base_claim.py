"""BaseClaimer + base_config: the airdrop lives on its own contract.

The Merkle reward drop (claimMerkle) goes to the merkle contract and the airdrop
(claimAirdrop & views) to the SatoriAirdrop contract, which is no longer part of
rewards. No network: web3 contract objects build calldata offline.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

pytest.importorskip("web3")

_PKG = Path(__file__).parent.parent / "neuron-lite" / "satorineuron"


_EVM = Path(__file__).parent.parent.parent / "satorilib" / "src" / "satorilib" / "chain" / "evm.py"


def _load(name, path=None):
    # By file path: the satorineuron package __init__ pulls heavy deps, and
    # other tests replace `satorilib` in sys.modules with a mock.
    spec = importlib.util.spec_from_file_location(f"_t_{name}", path or _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bc = _load("base_config")
BaseClaimer = _load("base_claim").BaseClaimer

MERKLE = "0x78C83AE275e9659Bc8490fa33943e22ec7c5A7e7"
AIRDROP = "0xF15CEAec0e29A0B6551180F8617850bc5356938C"
USER = "0x9f505692808622fc1F0fbb53324262162C788762"


@pytest.fixture
def claimer(monkeypatch):
    monkeypatch.setitem(sys.modules, "satorilib.chain.evm", _load("evm", _EVM))
    return BaseClaimer(rpc_url="http://127.0.0.1:1", merkle_address=MERKLE,
                       airdrop_address=AIRDROP)


def _fn_names(contract):
    return {f["name"] for f in contract.abi if f.get("type") == "function"}


def test_airdrop_calls_bound_to_airdrop_contract(claimer):
    assert claimer.airdrop.address == AIRDROP
    assert _fn_names(claimer.airdrop) == {
        "claimAirdrop", "claimableAirdrop", "airdropAllocation", "airdropClaimed"}
    data = claimer.airdrop.functions.claimAirdrop()._encode_transaction_data()
    assert data[:10] == "0x" + bytes(claimer.client.w3.keccak(text="claimAirdrop()"))[:4].hex()


def test_merkle_calls_bound_to_merkle_contract(claimer):
    assert claimer.contract.address == MERKLE
    names = _fn_names(claimer.contract)
    assert {"claimMerkle", "alreadyMintedTo"} <= names
    assert not names & {"claimAirdrop", "claimableAirdrop"}


def test_claim_airdrop_sends_to_airdrop(claimer, monkeypatch):
    sent = {}
    monkeypatch.setattr(claimer.client, "send",
                        lambda fn, private_key: sent.setdefault("to", fn.address) or "0xtx")
    claimer.claim_airdrop("0x" + "11" * 32)
    assert sent["to"] == AIRDROP


def test_claim_merkle_sends_to_merkle(claimer, monkeypatch):
    sent = {}
    monkeypatch.setattr(claimer.client, "send",
                        lambda fn, private_key: sent.setdefault("to", fn.address) or "0xtx")
    claimer.claim("0x" + "11" * 32, 10, ["0x" + "22" * 32])
    assert sent["to"] == MERKLE


def test_base_config_defaults_include_airdrop():
    assert bc.DEFAULTS["airdrop"] == AIRDROP
    assert bc.DEFAULTS["merkle"] == MERKLE
    assert bc._ENV["airdrop"] == "BASE_SATORI_AIRDROP"


def test_base_config_airdrop_env_override(monkeypatch):
    monkeypatch.setenv("SATORI_API_URL", "")
    monkeypatch.setattr(bc, "_central_url", lambda: None)
    monkeypatch.setenv("BASE_SATORI_AIRDROP", USER)
    cfg = bc.base_config(force=True)
    assert cfg["airdrop"] == USER
    bc._cache["data"] = None


# ── On-chain prediction rewards: read on the Lens, claim through the Hub ──

HUB = "0x1eec7E1443C1065ab78180A14326e5Ac8284f143"
LENS = "0x9BF5266F60092F1bFbd5F16d21d0bE9673c0F91F"


@pytest.fixture
def full_claimer(monkeypatch):
    monkeypatch.setitem(sys.modules, "satorilib.chain.evm", _load("evm", _EVM))
    return BaseClaimer(rpc_url="http://127.0.0.1:1", merkle_address=MERKLE,
                       airdrop_address=AIRDROP, hub_address=HUB, lens_address=LENS)


def test_prediction_rewards_contracts(full_claimer):
    assert full_claimer.hub.address == HUB
    assert full_claimer.lens.address == LENS
    data = full_claimer.hub.functions.claim()._encode_transaction_data()
    assert data[:10] == "0x" + bytes(full_claimer.client.w3.keccak(text="claim()"))[:4].hex()


def test_claim_prediction_rewards_sends_to_hub(full_claimer, monkeypatch):
    sent = {}
    monkeypatch.setattr(full_claimer.client, "send",
                        lambda fn, private_key: sent.setdefault("to", fn.address) or "0xtx")
    full_claimer.claim_prediction_rewards("0x" + "11" * 32)
    assert sent["to"] == HUB


def test_prediction_rewards_reads_lens(full_claimer, monkeypatch):
    seen = {}

    class Call:
        def __init__(self, user):
            seen["user"] = user

        def call(self):
            return (5 * 10 ** 18, False)

    monkeypatch.setattr(full_claimer.lens.functions, "getTotalClaimableRewards", Call)
    assert full_claimer.prediction_rewards(USER.lower()) == {"claimable": 5 * 10 ** 18, "complete": False}
    assert seen["user"] == USER


def test_hub_and_lens_optional(claimer):
    assert claimer.hub is None and claimer.lens is None


def test_base_config_defaults_include_lens():
    assert bc.DEFAULTS["lens"] == LENS
    assert bc._ENV["lens"] == "BASE_SATORI_LENS"
