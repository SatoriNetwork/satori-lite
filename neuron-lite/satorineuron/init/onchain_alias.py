"""Another alias for the Satori contract (on-chain scoring), beside the neuron's.

The neuron's alias (alias.yaml) does both jobs by default: the Satori Network
knows the neuron by it (off-chain scoring) and the Satori contract too (the
wallet stakes with it, it sends the daily batch). A person can give the
on-chain job another alias, such as their Satori app's (its Backup shows the
alias's private key): its private key goes in onchain-alias.yaml, and the
neuron then stakes and predicts on Base with it, while alias.yaml keeps the
off-chain job. Register reports each for its job (central's alias reports).

Keys are never deleted: one handed back is renamed, not removed.
"""

import os
import re
import time
from typing import Optional

import yaml
from eth_account import Account
from eth_account.messages import encode_defunct

from satorineuron import config

_KEY = re.compile(r'^(0x)?[0-9a-fA-F]{64}$')


class KeyAlias:
    """An alias from its private key: what the on-chain code and register use
    of one (ethAddress, account, pubkey, ethSign)."""

    def __init__(self, private_key: str):
        self.account = Account.from_key(private_key)
        self.ethAddress = self.account.address
        self.address = self.ethAddress
        self.pubkey = self.account._key_obj.public_key.to_compressed_bytes().hex()

    def ethSign(self, message: str) -> str:
        """EIP-191 (personal_sign) signature of message, 0x and r || s || v,
        as satorilib's alias signs."""
        return '0x' + bytes(self.account.sign_message(encode_defunct(text=message)).signature).hex()


def onchainAliasPath() -> str:
    return config.keysPath('onchain-alias.yaml')


def onchainLinkPath() -> str:
    ''' the wallet's link to the on-chain alias (nothing secret) '''
    return config.keysPath('onchain-wallet-link.yaml')


def readOnchainAlias(path: Optional[str] = None) -> Optional[KeyAlias]:
    """The on-chain alias, or None when the neuron's alias does that job."""
    path = path or onchainAliasPath()
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return KeyAlias(str(data['privateKey']))


def checkKey(text: str) -> str:
    """A private key as 0x and 64 hex, or ValueError."""
    t = (text or '').strip()
    if not _KEY.match(t):
        raise ValueError('Give the alias\'s private key: 64 hex characters, with or without 0x.')
    return '0x' + t[-64:].lower()


def _retiredPath(path: str) -> str:
    """A name no earlier retired key has: a key is never overwritten."""
    stem, n = f'{path[:-len(".yaml")]}.retired-{int(time.time())}', 0
    while os.path.exists(f'{stem}{f"-{n}" if n else ""}.yaml'):
        n += 1
    return f'{stem}{f"-{n}" if n else ""}.yaml'


def setOnchainAlias(private_key: Optional[str], *, own_alias: str, wallet: Optional[str] = None,
                    path: Optional[str] = None) -> Optional[KeyAlias]:
    """Store another alias for the Satori contract (its private key), or hand
    the job back to the neuron's alias (None or ''). `own_alias`, `wallet`:
    those addresses, refused as the on-chain alias (own_alias means: hand
    back). The one it replaces is kept, renamed. Returns the on-chain alias,
    or None."""
    path = path or onchainAliasPath()
    current = readOnchainAlias(path)
    alias = KeyAlias(checkKey(private_key)) if (private_key or '').strip() else None
    if alias is not None and wallet and alias.ethAddress.lower() == wallet.lower():
        raise ValueError("That is the wallet's own key: give an alias's.")
    if alias is not None and alias.ethAddress.lower() == own_alias.lower():
        alias = None
    if alias is None and current is None:
        raise ValueError('The neuron already uses its own alias for the Satori contract.')
    if alias is not None and current is not None and alias.ethAddress == current.ethAddress:
        raise ValueError('That is the alias the neuron uses for the Satori contract now.')
    if current is not None:
        os.rename(path, _retiredPath(path))
    if alias is None:
        return None
    with open(path, 'w') as f:
        yaml.safe_dump({'privateKey': alias.account.key.to_0x_hex(), 'address': alias.ethAddress},
                       f, default_flow_style=False, sort_keys=False)
    os.chmod(path, 0o600)
    return alias
