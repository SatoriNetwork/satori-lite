"""BaseClaimer + base_config: the airdrop lives on its own contract.

The Merkle reward drop (claimMerkle) goes to the merkle contract and the airdrop
(claimAirdrop & views) to the SatoriAirdrop contract, which is no longer part of
rewards. No network: web3 contract objects build calldata offline.
"""

import pytest

pytest.importorskip("web3")

from satorineuron import base_config as bc  # noqa: E402
from satorineuron.base_claim import BaseClaimer  # noqa: E402

# The claimer's addresses are the config's own defaults, what a neuron that cannot reach Central
# claims on (web/routes.py, _base_claimer): a copy of a deployment here went stale at each redeploy.
MERKLE, AIRDROP, HUB, LENS, TOKEN = (bc.DEFAULTS[k] for k in ("merkle", "airdrop", "hub", "lens", "token"))
USER = "0x9f505692808622fc1F0fbb53324262162C788762"


@pytest.fixture
def claimer():
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
    # Its own contract: on the merkle's or the rewards' address the binding tests above prove nothing.
    assert AIRDROP not in (MERKLE, bc.DEFAULTS["rewards"])
    assert bc._ENV["airdrop"] == "BASE_SATORI_AIRDROP"


def test_base_config_airdrop_env_override(monkeypatch):
    monkeypatch.setenv("SATORI_API_URL", "")
    monkeypatch.setattr(bc, "_central_url", lambda: None)
    monkeypatch.setenv("BASE_SATORI_AIRDROP", USER)
    monkeypatch.setattr(bc, "_cache", {"data": None, "ts": 0.0})  # the module is every test's now
    assert bc.base_config()["airdrop"] == USER


# ── On-chain prediction rewards: read on the Lens, claim through the Hub ──

@pytest.fixture
def full_claimer():
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
    assert LENS not in (HUB, bc.DEFAULTS["rewards"])
    assert bc._ENV["lens"] == "BASE_SATORI_LENS"


# ── Gas: a claim is sent from the claiming address, which needs ETH ──

def test_eth_balance_reads_the_address(claimer):
    seen = {}

    class Eth:
        def get_balance(self, address):
            seen["address"] = address
            return 7

    claimer.client._w3 = type("W3", (), {"eth": Eth()})()
    assert claimer.eth_balance(USER.lower()) == 7
    assert seen["address"] == USER


# ── Balances: SATORI on the token contract ──

def test_token_balance_reads_balance_of(monkeypatch):
    claimer = BaseClaimer(rpc_url="http://127.0.0.1:1", merkle_address=MERKLE,
                          airdrop_address=AIRDROP, token_address=TOKEN)
    assert claimer.token.address == TOKEN
    seen = {}

    class Call:
        def __init__(self, account):
            seen["account"] = account

        def call(self):
            return 3 * 10 ** 18

    monkeypatch.setattr(claimer.token.functions, "balanceOf", Call)
    assert claimer.token_balance(USER.lower()) == 3 * 10 ** 18
    assert seen["account"] == USER


def test_token_optional(claimer):
    assert claimer.token is None
