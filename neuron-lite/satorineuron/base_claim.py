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
        hardcodes a deployment — a Base redeploy changes only central's config."""
        from web3 import Web3

        self.rpc_url = rpc_url
        self.merkle_address = Web3.to_checksum_address(merkle_address)
        self.rewards_address = Web3.to_checksum_address(rewards_address)
        self.w3 = Web3(Web3.HTTPProvider(self.rpc_url))
        self.contract = self.w3.eth.contract(address=self.merkle_address, abi=MERKLE_ABI)
        self.rewards = self.w3.eth.contract(address=self.rewards_address, abi=REWARDS_ABI)

    def _send(self, private_key: str, fn) -> str:
        """Build, sign (with the vault key), send and confirm a contract call.
        Returns the 0x tx hash. Raises on send/revert (e.g. no gas)."""
        account = self.w3.eth.account.from_key(private_key)
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
            raise RuntimeError(f"transaction reverted: {h}")
        return h

    # ---- Merkle reward drop ------------------------------------------------
    def already_minted(self, address: str) -> int:
        """Wei already minted to `address` through the Merkle channel."""
        from web3 import Web3
        return int(self.contract.functions.alreadyMintedTo(
            Web3.to_checksum_address(address)).call())

    def claim(self, private_key: str, lifetime_entitlement: int, proof: List[str]) -> str:
        """Submit claimMerkle(lifetimeEntitlement, proof) signed by the vault
        key. Mints the delta over what's already been claimed to msg.sender."""
        return self._send(private_key, self.contract.functions.claimMerkle(
            int(lifetime_entitlement), list(proof)))

    # ---- Airdrop -----------------------------------------------------------
    def airdrop_status(self, address: str) -> dict:
        """{allocation, claimed, claimable} in wei for `address`. `claimable` is
        the vested-and-unclaimed amount (the contract applies the vest curve)."""
        from web3 import Web3
        a = Web3.to_checksum_address(address)
        return {
            "allocation": int(self.rewards.functions.airdropAllocation(a).call()),
            "claimed": int(self.rewards.functions.airdropClaimed(a).call()),
            "claimable": int(self.rewards.functions.claimableAirdrop(a).call()),
        }

    def claim_airdrop(self, private_key: str) -> str:
        """Submit claimAirdrop() signed by the vault key. Mints the vested,
        unclaimed airdrop to msg.sender. No proof needed."""
        return self._send(private_key, self.rewards.functions.claimAirdrop())
