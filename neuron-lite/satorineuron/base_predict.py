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

    def __init__(self, rpc_url: str, engine_address: str, games_address: str):
        from satorilib.chain.evm import EvmClient
        self.client = EvmClient(rpc_url)
        self.engine = self.client.contract(engine_address, ENGINE_ABI)
        self.games = self.client.contract(games_address, GAMES_ABI)

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
