"""Claim Base reward drops from the neuron itself.

The neuron holds the vault key (it signs predictions), so it can also submit the
`claimMerkle` transaction directly — no MetaMask, no dapp. It reads how much has
already been minted to the user's Base address, and the claim mints the delta up
to their published lifetime entitlement.

Gas: the claim is sent from the user's Base address, so that address needs a
little ETH on Base. If it has none, the send fails with a clear error.
"""

import logging
import os
from typing import List

logger = logging.getLogger(__name__)

DEFAULT_RPC_URL = "https://sepolia.base.org"
DEFAULT_MERKLE = "0xbA81c904b533C1B0e006c35A46bee74F75239AFA"  # Base Sepolia SatoriMerkle

MERKLE_ABI = [
    {
        "name": "claimMerkle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "lifetimeEntitlement", "type": "uint256"},
            {"name": "proof", "type": "bytes32[]"},
        ],
        "outputs": [],
    },
    {
        "name": "alreadyMintedTo",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "merkleRoot",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]


class BaseClaimer:
    """Reads claim state and submits claimMerkle on Base."""

    def __init__(self, rpc_url: str = None, merkle_address: str = None):
        from web3 import Web3

        self.rpc_url = rpc_url or os.getenv("BASE_RPC_URL", DEFAULT_RPC_URL)
        self.merkle_address = Web3.to_checksum_address(
            merkle_address or os.getenv("BASE_SATORI_MERKLE", DEFAULT_MERKLE)
        )
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.contract = self.w3.eth.contract(address=self.merkle_address, abi=MERKLE_ABI)

    def already_minted(self, address: str) -> int:
        """Wei already minted to `address` through the Merkle channel."""
        from web3 import Web3
        return int(self.contract.functions.alreadyMintedTo(
            Web3.to_checksum_address(address)).call())

    def claim(self, private_key: str, lifetime_entitlement: int, proof: List[str]) -> str:
        """Submit claimMerkle(lifetimeEntitlement, proof) signed by `private_key`
        (the vault key). Mints the delta over what's already been claimed to
        msg.sender. Returns the tx hash. Raises on send/revert (e.g. no gas)."""
        account = self.w3.eth.account.from_key(private_key)
        fn = self.contract.functions.claimMerkle(int(lifetime_entitlement), list(proof))
        gas_price = self.w3.eth.gas_price
        tx = fn.build_transaction({
            "from": account.address,
            "nonce": self.w3.eth.get_transaction_count(account.address),
            "chainId": self.w3.eth.chain_id,
            "maxFeePerGas": gas_price * 2,
            "maxPriorityFeePerGas": gas_price,
        })
        signed = account.sign_transaction(tx)
        txhash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(txhash)
        h = txhash.hex()
        h = h if h.startswith("0x") else "0x" + h
        if receipt.status != 1:
            raise RuntimeError(f"claim transaction reverted: {h}")
        return h
