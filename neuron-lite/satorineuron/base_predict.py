"""Submit predictions on-chain to Base — the neuron's on-chain prediction path.

Separate from the central path (which posts to the server) and from the relay
path (which publishes {stream}_pred to Nostr). This one signs, with the vault
key, a single batched `predictMultipleWithPayloads` on the engine — at most once
per UTC day (the contract enforces one batch per round, globally), gated by a
user toggle because it spends gas.

The on-chain prediction is a DIRECTION, not a value: payload byte 0x01 = UP,
0x02 = DOWN. We derive it from the engine's numeric forecast vs the stream's
latest value. You predict on a gameId, resolved from the stream's on-chain
streamId via games.binaryGameByStream.
"""

import logging
import re
from typing import List, Optional, Tuple

from eth_utils import to_checksum_address

logger = logging.getLogger(__name__)

UP = 1
DOWN = 2

# satori-<chainId>-<streamId>  (e.g. satori-84532-1)
_BASE_STREAM_RE = re.compile(r"^satori-(\d+)-(\d+)$")

ENGINE_ABI = [
    {
        "name": "predictMultipleWithPayloads",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{
            "name": "requests",
            "type": "tuple[]",
            "components": [
                {"name": "gameId", "type": "uint32"},
                {"name": "payload", "type": "bytes"},
            ],
        }],
        "outputs": [],
    },
    {
        "name": "predictedStreamsCount",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint256"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

GAMES_ABI = [
    {
        "name": "binaryGameByStream",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "uint32"}],
        "outputs": [{"name": "", "type": "uint32"}],
    },
]

# Delegation: the vault delegates its prediction power to the identity wallet
# (on the hub) so the neuron can predict with the always-available identity key
# while rewards still accrue to the vault. The identity's predictor fee is set
# to 0 (on rewards) so it takes no cut of the vault's delegated rewards.
HUB_ABI = [
    {
        "name": "delegatePredictionPower",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "delegate", "type": "address"}],
        "outputs": [],
    },
]

TOKEN_ABI = [
    {
        "name": "transferLockedUntil",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [{"name": "", "type": "uint24"}],
    },
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_LOCK_INDEFINITE = (1 << 24) - 1  # type(uint24).max — the "locked while delegated" sentinel

REWARDS_ABI = [
    {
        "name": "setPredictorFee",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "bps", "type": "uint16"}],
        "outputs": [],
    },
    {
        "name": "effectivePredictorFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "predictor", "type": "address"}],
        "outputs": [{"name": "", "type": "uint16"}],
    },
    {
        "name": "predictionDelegateInfo",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [
            {"name": "delegate", "type": "address"},
            {"name": "power", "type": "uint96"},
        ],
    },
]

TIME_UNIT_SECONDS = 86400  # on-chain round = 1 UTC day (SatoriToken.TIME_UNIT_SECONDS)


def parse_base_stream(stream_name: str) -> Optional[Tuple[int, int]]:
    """(chainId, streamId) if the name is a base stream (satori-<chainId>-<streamId>), else None."""
    m = _BASE_STREAM_RE.match(stream_name or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def direction_from(forecast, latest) -> int:
    """UP if the forecast is above the latest value, else DOWN. (Ties → DOWN.)"""
    return UP if float(forecast) > float(latest) else DOWN


def build_requests(directions: dict, predictor) -> List[Tuple[int, int]]:
    """Turn {streamId: direction} into [(gameId, direction)] for the batched
    predict, resolving each stream's binary gameId and skipping streams with no
    game (gameId 0) or a resolution error."""
    requests = []
    for stream_id, direction in directions.items():
        try:
            game_id = predictor.game_for_stream(stream_id)
        except Exception as e:
            logger.warning("base predict: could not resolve game for stream %s: %s", stream_id, e)
            continue
        if game_id and int(game_id) != 0:
            requests.append((int(game_id), int(direction)))
    return requests


class BasePredictor:
    """Reads round/game state and submits the batched on-chain prediction."""

    def __init__(self, rpc_url: str, engine_address: str, games_address: str,
                 hub_address: str = None, rewards_address: str = None,
                 token_address: str = None):
        from satorilib.chain.evm import EvmClient
        self.client = EvmClient(rpc_url)
        self.engine = self.client.contract(engine_address, ENGINE_ABI)
        self.games = self.client.contract(games_address, GAMES_ABI)
        # hub/rewards/token only needed for delegation setup + stake status.
        self.hub = self.client.contract(hub_address, HUB_ABI) if hub_address else None
        self.rewards = self.client.contract(rewards_address, REWARDS_ABI) if rewards_address else None
        self.token = self.client.contract(token_address, TOKEN_ABI) if token_address else None

    def current_round(self) -> int:
        """UTC-day round index (matches SatoriToken.getCurrentRound = block.timestamp/86400)."""
        return self.client.latest_timestamp() // TIME_UNIT_SECONDS

    def already_predicted(self, address: str) -> bool:
        """True if this address has already submitted its one batch this round."""
        count = self.engine.functions.predictedStreamsCount(
            to_checksum_address(address), self.current_round()).call()
        return int(count) != 0

    def game_for_stream(self, stream_id: int) -> int:
        """The stream's default binary gameId (0 = none exists → not predictable)."""
        return int(self.games.functions.binaryGameByStream(int(stream_id)).call())

    def predict(self, private_key: str, requests: List[Tuple[int, int]]) -> str:
        """Submit ONE batch. `requests` = [(gameId, direction 1|2), ...]. Signs
        with the vault key. Returns the 0x tx hash. Raises on send/revert."""
        if not requests:
            raise ValueError("no predictions to submit")
        payloads = [(int(game_id), bytes([int(direction)])) for game_id, direction in requests]
        return self.client.send(
            self.engine.functions.predictMultipleWithPayloads(payloads),
            private_key=private_key)

    # ---- Delegation setup (one-time) ---------------------------------------
    def incoming_delegated_power(self, address: str) -> int:
        """Prediction power delegated TO `address` by others (the vault's power,
        once set up). 0 means no delegation yet — predicting would earn nothing."""
        _delegate, power = self.rewards.functions.predictionDelegateInfo(
            to_checksum_address(address)).call()
        return int(power)

    def current_delegate(self, address: str) -> str:
        """Whom `address` has delegated its prediction power TO (0x0 if none)."""
        delegate, _power = self.rewards.functions.predictionDelegateInfo(
            to_checksum_address(address)).call()
        return delegate

    def effective_predictor_fee(self, address: str) -> int:
        """`address`'s effective predictor-fee bps (its cut as a delegate)."""
        return int(self.rewards.functions.effectivePredictorFee(
            to_checksum_address(address)).call())

    def set_predictor_fee_zero(self, private_key: str) -> str:
        """Set the caller's predictor fee to 0% (setPredictorFee(0) — the sentinel
        that decodes to 0). Set-once on the contract."""
        return self.client.send(
            self.rewards.functions.setPredictorFee(0), private_key=private_key)

    def delegate_to(self, private_key: str, delegate_address: str) -> str:
        """Delegate the signer's prediction power to `delegate_address` (hub)."""
        return self.client.send(
            self.hub.functions.delegatePredictionPower(to_checksum_address(delegate_address)),
            private_key=private_key)

    def ensure_delegation(self, vault_key: str, vault_address: str,
                          identity_key: str, identity_address: str) -> dict:
        """Idempotent one-time setup: the identity's cut → 0% (identity signs),
        THEN the vault delegates its prediction power to the identity (vault
        signs). Reads on-chain state first so we never spend gas re-doing a step
        that's already in place. Signing keys are only needed here (setup)."""
        result = {}
        # 1. identity predictor fee -> 0 (before delegation)
        if self.effective_predictor_fee(identity_address) != 0:
            result["fee_tx"] = self.set_predictor_fee_zero(identity_key)
        else:
            result["fee"] = "already 0"
        # 2. vault delegates to identity
        if self.current_delegate(vault_address).lower() != identity_address.lower():
            result["delegate_tx"] = self.delegate_to(vault_key, identity_address)
        else:
            result["delegate"] = "already delegated"
        return result

    def undelegate(self, vault_key: str) -> str:
        """Undelegate (delegate to address(0)). Frees the vault's prediction
        power and switches the token lock from indefinite to next-round (~24h)."""
        return self.delegate_to(vault_key, ZERO_ADDRESS)

    def ensure_undelegated(self, vault_key: str, vault_address: str) -> dict:
        """Idempotent: undelegate the vault if it's currently delegated."""
        if int(self.current_delegate(vault_address), 16) == 0:
            return {"delegate": "already undelegated"}
        return {"undelegate_tx": self.undelegate(vault_key)}

    def transfer_locked_until(self, address: str) -> int:
        """Round until which `address`'s tokens are transfer-locked (0 = free)."""
        return int(self.token.functions.transferLockedUntil(
            to_checksum_address(address)).call())

    def stake_status(self, vault_address: str) -> dict:
        """Current staking state for the vault: delegated?, token locked?, and
        (when unstaking) the round/timestamp the tokens unlock."""
        delegate = self.current_delegate(vault_address)
        staked = int(delegate, 16) != 0
        lock_round = self.transfer_locked_until(vault_address)
        current = self.current_round()
        indefinite = lock_round >= _LOCK_INDEFINITE
        locked = lock_round > current
        return {
            "staked": staked,
            "locked": locked,
            "indefinite": indefinite,
            "unlock_round": None if (indefinite or not locked) else lock_round,
            "unlock_ts": None if (indefinite or not locked) else lock_round * TIME_UNIT_SECONDS,
        }
