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

# The airdrop is its own contract (SatoriAirdrop), separate from rewards.
AIRDROP_ABI = [
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

# On-chain prediction rewards: credited on SatoriRewards when a prediction is
# scored, read through the Lens, minted when the vault calls SatoriHub.claim().
LENS_ABI = [
    {
        "name": "getTotalClaimableRewards",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "user", "type": "address"}],
        "outputs": [{"name": "total", "type": "uint256"},
                    {"name": "complete", "type": "bool"}],
    },
]

# The SATORI ERC-20 itself, for reading balances.
TOKEN_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]

HUB_ABI = [
    {
        "name": "claim",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
]


class BaseClaimer:
    """Reads claim state and submits claims on Base — the Merkle reward drop
    (claimMerkle, needs a proof from central), the airdrop (claimAirdrop, no
    proof; the contract computes vesting) and on-chain prediction rewards
    (SatoriHub.claim)."""

    def __init__(self, rpc_url: str, merkle_address: str, airdrop_address: str,
                 hub_address: str = None, lens_address: str = None,
                 token_address: str = None):
        """Addresses are injected (from satorineuron.base_config) so nothing here
        hardcodes a deployment — a Base redeploy changes only central's config.
        The web3 tx plumbing lives in the shared satorilib.chain.evm.EvmClient."""
        from satorilib.chain.evm import EvmClient
        self.client = EvmClient(rpc_url)
        self.contract = self.client.contract(merkle_address, MERKLE_ABI)
        self.airdrop = self.client.contract(airdrop_address, AIRDROP_ABI)
        self.hub = self.client.contract(hub_address, HUB_ABI) if hub_address else None
        self.lens = self.client.contract(lens_address, LENS_ABI) if lens_address else None
        self.token = self.client.contract(token_address, TOKEN_ABI) if token_address else None

    # ---- Merkle reward drop ------------------------------------------------
    def already_minted(self, address: str) -> int:
        """Wei already minted to `address` through the Merkle channel."""
        return int(self.contract.functions.alreadyMintedTo(
            to_checksum_address(address)).call())

    def eth_balance(self, address: str) -> int:
        """Wei of ETH at `address`, which pays the gas of a claim sent from it."""
        return int(self.client.w3.eth.get_balance(to_checksum_address(address)))

    def token_balance(self, address: str) -> int:
        """Wei of SATORI at `address`. Staked SATORI stays in the vault (it is
        only delegated and transfer-locked), so this includes it."""
        return int(self.token.functions.balanceOf(to_checksum_address(address)).call())

    def claim(self, private_key: str, lifetime_entitlement: int, proof: List[str]) -> str:
        """Submit claimMerkle(lifetimeEntitlement, proof) signed by the vault
        (or identity) key. Mints the delta over what's already been claimed to
        msg.sender."""
        return self.client.send(
            self.contract.functions.claimMerkle(int(lifetime_entitlement), list(proof)),
            private_key=private_key)

    # ---- Airdrop -----------------------------------------------------------
    def airdrop_status(self, address: str) -> dict:
        """{allocation, claimed, claimable} in wei for `address`. `claimable` is
        the vested-and-unclaimed amount (the contract applies the vest curve)."""
        a = to_checksum_address(address)
        return {
            "allocation": int(self.airdrop.functions.airdropAllocation(a).call()),
            "claimed": int(self.airdrop.functions.airdropClaimed(a).call()),
            "claimable": int(self.airdrop.functions.claimableAirdrop(a).call()),
        }

    def claim_airdrop(self, private_key: str) -> str:
        """Submit claimAirdrop() signed by the vault key. Mints the vested,
        unclaimed airdrop to msg.sender. No proof needed."""
        return self.client.send(
            self.airdrop.functions.claimAirdrop(), private_key=private_key)

    # ---- On-chain prediction rewards ---------------------------------------
    def prediction_rewards(self, address: str) -> dict:
        """{claimable, complete} for `address`: claimable is wei credited from
        scored predictions plus the vault's pending delegator share. complete is
        False when the Lens hit its round budget, so the amount is a lower bound."""
        total, complete = self.lens.functions.getTotalClaimableRewards(
            to_checksum_address(address)).call()
        return {"claimable": int(total), "complete": bool(complete)}

    def claim_prediction_rewards(self, private_key: str) -> str:
        """Submit SatoriHub.claim() signed by the vault key. Mints the vault's
        prediction rewards to itself."""
        return self.client.send(self.hub.functions.claim(), private_key=private_key)
