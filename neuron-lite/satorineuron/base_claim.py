"""Claim Base reward drops from the neuron itself.

The neuron holds the vault key (it signs predictions), so it can also submit the
`claimMerkle` transaction directly — no MetaMask, no dapp. It reads how much has
already been minted to the user's Base address, and the claim mints the delta up
to their published lifetime entitlement.

Gas: the claim is sent from the user's Base address, so that address needs a
little ETH on Base. If it has none, the send fails with a clear error.
"""

import logging
from typing import List

from eth_utils import to_checksum_address

logger = logging.getLogger(__name__)

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

REWARDS_ABI = [
    {
        "name": "claimAirdrop",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "claimableAirdrop",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "airdropAllocation",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "airdropClaimed",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class BaseClaimer:
    """Reads claim state and submits claims on Base — the Merkle reward drop
    (claimMerkle, needs a proof from central) and the airdrop (claimAirdrop, no
    proof; the contract computes vesting)."""

    def __init__(self, rpc_url: str, merkle_address: str, rewards_address: str):
        """Addresses are injected (from satorineuron.base_config) so nothing here
        hardcodes a deployment — a Base redeploy changes only central's config.
        The web3 tx plumbing lives in the shared satorilib.chain.evm.EvmClient."""
        from satorilib.chain.evm import EvmClient
        self.client = EvmClient(rpc_url)
        self.contract = self.client.contract(merkle_address, MERKLE_ABI)
        self.rewards = self.client.contract(rewards_address, REWARDS_ABI)

    # ---- Merkle reward drop ------------------------------------------------
    def already_minted(self, address: str) -> int:
        """Wei already minted to `address` through the Merkle channel."""
        return int(self.contract.functions.alreadyMintedTo(
            to_checksum_address(address)).call())

    def claim(self, private_key: str, lifetime_entitlement: int, proof: List[str]) -> str:
        """Submit claimMerkle(lifetimeEntitlement, proof) signed by the vault
        key. Mints the delta over what's already been claimed to msg.sender."""
        return self.client.send(
            self.contract.functions.claimMerkle(int(lifetime_entitlement), list(proof)),
            private_key=private_key)

    # ---- Airdrop -----------------------------------------------------------
    def airdrop_status(self, address: str) -> dict:
        """{allocation, claimed, claimable} in wei for `address`. `claimable` is
        the vested-and-unclaimed amount (the contract applies the vest curve)."""
        a = to_checksum_address(address)
        return {
            "allocation": int(self.rewards.functions.airdropAllocation(a).call()),
            "claimed": int(self.rewards.functions.airdropClaimed(a).call()),
            "claimable": int(self.rewards.functions.claimableAirdrop(a).call()),
        }

    def claim_airdrop(self, private_key: str) -> str:
        """Submit claimAirdrop() signed by the vault key. Mints the vested,
        unclaimed airdrop to msg.sender. No proof needed."""
        return self.client.send(
            self.rewards.functions.claimAirdrop(), private_key=private_key)
