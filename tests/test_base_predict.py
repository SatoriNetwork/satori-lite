"""base_predict: a named game takes one prediction per 24 clock hours.

The engine reverts the WHOLE batch with DuplicatePrediction when a named game
in it was predicted less than 24 clock hours ago, so the neuron leaves named
games out when its own last submit was too recent, and resends once without
them if the chain still says too soon (a schedule lost to a reinstall).
"""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("web3")

_PKG = Path(__file__).parent.parent / "neuron-lite" / "satorineuron"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_t_{name}", _PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bp = _load("base_predict")
HOUR = 3600


class ContractCustomError(Exception):
    """Shape of web3's: the raw revert selector as message and `data`."""

    def __init__(self, data):
        super().__init__(data)
        self.data = data


class FakePredictor:
    def __init__(self, refuse_named=False, error=None):
        self.sent = []
        self.refuse_named = refuse_named
        self.error = error

    def predict(self, private_key, requests):
        self.sent.append(list(requests))
        if self.error:
            raise self.error
        if self.refuse_named and any(g in (7, 8) for g, _ in requests):
            raise ContractCustomError(bp.DUPLICATE_PREDICTION)
        return "0xtx"


BATCH = [(3, bp.UP, False), (7, bp.DOWN, True), (8, bp.UP, True)]


def test_selector_is_duplicate_prediction():
    from eth_utils import keccak
    assert bp.DUPLICATE_PREDICTION == "0x" + keccak(text="DuplicatePrediction()")[:4].hex()


@pytest.mark.parametrize("last,now,ok", [
    (0, 100 * HOUR, True),                            # never submitted: unknown, try
    (100 * HOUR + 1800, 124 * HOUR, True),            # 24 clock hours later
    (100 * HOUR + 1800, 124 * HOUR - 1, False),       # one second short of the hour
    (100 * HOUR + 3599, 124 * HOUR + 10, True),       # the loop's +24h lands in hour 124
])
def test_named_allowed_counts_clock_hours(last, now, ok):
    assert bp.named_allowed(last, now) is ok


def test_named_left_out_when_last_submit_too_recent():
    p = FakePredictor()
    assert bp.submit_batch(p, "k", BATCH, named_ok=False) == "0xtx"
    assert p.sent == [[(3, bp.UP)]]


def test_whole_batch_when_named_allowed():
    p = FakePredictor()
    bp.submit_batch(p, "k", BATCH, named_ok=True)
    assert p.sent == [[(3, bp.UP), (7, bp.DOWN), (8, bp.UP)]]


def test_resends_without_named_when_chain_says_too_soon():
    p = FakePredictor(refuse_named=True)
    assert bp.submit_batch(p, "k", BATCH, named_ok=True) == "0xtx"
    assert p.sent == [[(3, bp.UP), (7, bp.DOWN), (8, bp.UP)], [(3, bp.UP)]]


def test_nothing_left_when_every_game_is_named():
    p = FakePredictor()
    assert bp.submit_batch(p, "k", [(7, bp.UP, True)], named_ok=False) is None
    assert p.sent == []


def test_other_errors_are_not_retried():
    p = FakePredictor(error=RuntimeError("transaction reverted: 0xdeadbeef"))
    with pytest.raises(RuntimeError):
        bp.submit_batch(p, "k", BATCH, named_ok=True)
    assert len(p.sent) == 1


def test_duplicate_without_named_games_is_not_retried():
    p = FakePredictor(error=ContractCustomError(bp.DUPLICATE_PREDICTION))
    with pytest.raises(ContractCustomError):
        bp.submit_batch(p, "k", [(3, bp.UP, False)], named_ok=True)
    assert len(p.sent) == 1


def test_registry_without_named_streams_reads_not_named():
    from types import SimpleNamespace

    class Reverting:
        def call(self):
            raise ValueError("execution reverted")

    p = bp.BasePredictor.__new__(bp.BasePredictor)
    p.registry = SimpleNamespace(functions=SimpleNamespace(isNamedStream=lambda sid: Reverting()))
    assert p.is_named(4) is False
    p.registry = None
    assert p.is_named(4) is False


# ── Why the neuron has no prediction power ───────────────────────────────

VAULT_PUBKEY = "026ebe87ebcc56db4ce7423b97718deed6e0e2248a27a063e21ca0ffa4bd4dc3d5"
VAULT = "0x03AbFED741a344C578304040D6CACAc86960e97d"
IDENTITY = "0xe8Dfe519a2351b37D662d42D09fb481Baab91FFF"
ZERO = "0x0000000000000000000000000000000000000000"


class DelegateOf:
    def __init__(self, delegate=None, error=None):
        self.delegate, self.error = delegate, error

    def current_delegate(self, address):
        if self.error:
            raise self.error
        return self.delegate


def test_vault_base_address_from_its_evrmore_pubkey():
    # A real tester vault: this pubkey's Base address is the one delegating on chain.
    assert bp.evm_address_from_pubkey(VAULT_PUBKEY) == VAULT
    assert bp.evm_address_from_pubkey("0x" + VAULT_PUBKEY) == VAULT


def test_reason_staked_but_no_satori():
    msg = bp.no_power_reason(DelegateOf(IDENTITY.lower()), IDENTITY, VAULT)
    assert "holds no SATORI" in msg and VAULT in msg


def test_reason_not_staked():
    assert "not staked" in bp.no_power_reason(DelegateOf(ZERO), IDENTITY, VAULT)


def test_reason_staked_to_someone_else():
    other = "0x9f505692808622fc1F0fbb53324262162C788762"
    msg = bp.no_power_reason(DelegateOf(other), IDENTITY, VAULT)
    assert other in msg and "restake" in msg


def test_reason_generic_without_vault_or_on_rpc_error():
    generic = bp.no_power_reason(DelegateOf(IDENTITY), IDENTITY, None)
    assert "no prediction power" in generic
    assert bp.no_power_reason(DelegateOf(error=RuntimeError("rpc")), IDENTITY, VAULT) == generic
