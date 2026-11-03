"""
The alias derived from the wallet, as the Satori app derives it (the owner,
2026-11-03: a wallet imported into the app made another alias, and the two
fought over the wallet's link).

The app's 24 words are the wallet's entropy (its private key) as BIP-39 words;
their seed (no passphrase) is the BIP-32 master, and the alias is
m/44'/60'/1'/0'/0' (satori/docs/design/wallets.md#derived-keys). So the
neuron and the app run one alias for one wallet, whichever imports it.

The switch happens when the wallet is unlocked (the only time its key is
here): the new alias file is written and checked, the old one is kept as
<file>.retired-<time> (keys are never deleted; its payment channels' scripts
beside it, the same way), the wallet links the new
alias, then (moveOnBase) the old alias's ETH goes to the new one and a stake
with the old alias moves to the new one. The neuron then restarts so every
part of it uses the new alias.
"""
import base64
import os
import shutil
import time
from typing import Optional

import yaml
from satorilib import logging

ALIAS_PATH = "m/44'/60'/1'/0'/0'"
ETH_TRANSFER_GAS = 21_000


def aliasKeyFromWallet(entropy: bytes) -> bytes:
    """The alias's private key (32 bytes) from the wallet's entropy, as the app makes it."""
    from eth_account import Account
    from eth_account.hdaccount.mnemonic import Mnemonic
    Account.enable_unaudited_hdwallet_features()
    words = Mnemonic('english').to_mnemonic(entropy)
    return bytes(Account.from_mnemonic(words, account_path=ALIAS_PATH).key)


def addressOf(key: bytes) -> str:
    from eth_account import Account
    return Account.from_key(key).address


def writeAliasFile(path: str, key: bytes) -> str:
    """
    The alias file for `key` at `path` (which must not exist), in satorilib's
    form (entropy, words, private key, public key, address), read back to check
    it. → its Base address
    """
    from satorilib.wallet.evrmore.identity import EvrmoreIdentity
    if os.path.exists(path):
        raise FileExistsError(path)
    seed = f'{path[:-len(".yaml")]}-seed.yaml'  # satorilib reads a key file by a .yaml name only
    with open(seed, 'w') as f:
        yaml.safe_dump({'entropy': base64.b64encode(key).decode()}, f)
    try:
        EvrmoreIdentity(seed).save(path)
    finally:
        os.remove(seed)  # the key only, now in `path` too
    written = EvrmoreIdentity(path)
    if not written.isDecrypted or bytes(written._entropy) != key:
        raise RuntimeError(f'the derived alias file at {path} did not read back')
    return addressOf(key)


def switchToDerivedAlias(manager) -> Optional[dict]:
    """
    With the wallet unlocked: when the alias is not the one derived from the
    wallet, the derived one takes its place (the old file kept, retired) and
    the wallet links it. → None when it already is (or the wallet is locked),
    else {'old', 'oldKey', 'new', 'newKey', 'retired'} (keys as 0x hex).
    """
    from satorilib.wallet.link import makeLink
    from satorineuron.init.wallet import writeWalletLink
    wallet, alias = manager.wallet, manager.alias
    if wallet is None or not wallet.isDecrypted or alias is None:
        return None
    key = aliasKeyFromWallet(bytes(wallet.identity._entropy))
    new = addressOf(key)
    if alias.ethAddress.lower() == new.lower():
        return None
    path = alias.identity.walletPath
    old, oldKey = alias.ethAddress, alias.account.key.to_0x_hex()
    pending = f'{path[:-len(".yaml")]}.derived.yaml'  # satorilib reads a key file by a .yaml name only
    if os.path.exists(pending):
        os.remove(pending)  # an earlier try's, never used: the same derived key
    writeAliasFile(pending, key)
    at = int(time.time())
    retired = f'{path}.retired-{at}'
    shutil.copy2(path, retired)
    # its payment channels' scripts go with it (the new alias is no party to them): set aside beside it, never deleted
    scripts = f'{path[:-len(".yaml")]}.scripts.json'
    if os.path.exists(scripts):
        os.replace(scripts, f'{scripts}.retired-{at}')
    os.replace(pending, path)
    manager.reloadAlias()
    writeWalletLink(makeLink(wallet, new))
    logging.info(f'the alias is now the one derived from the wallet: {new} (was {old}, kept at {retired})', color='green')
    return {'old': old, 'oldKey': oldKey, 'new': new, 'newKey': '0x' + key.hex(), 'retired': retired}


def sweepEth(client, fromKey: str, to: str) -> Optional[str]:
    """All of `fromKey`'s ETH to `to`, less the transfer's fee. → the tx hash, or None when it can't pay the fee."""
    from web3 import Web3
    w3 = client.w3
    account = w3.eth.account.from_key(fromKey)
    balance = w3.eth.get_balance(account.address)
    gasPrice = w3.eth.gas_price
    maxFee = gasPrice * 2
    value = balance - ETH_TRANSFER_GAS * maxFee
    if value <= 0:
        return None
    tx = {'from': account.address, 'to': Web3.to_checksum_address(to), 'value': value, 'gas': ETH_TRANSFER_GAS,
          'nonce': w3.eth.get_transaction_count(account.address, 'pending'), 'chainId': w3.eth.chain_id,
          'maxFeePerGas': maxFee, 'maxPriorityFeePerGas': gasPrice}
    h = w3.eth.send_raw_transaction(account.sign_transaction(tx).raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(h)
    h = h.hex() if h.hex().startswith('0x') else '0x' + h.hex()
    if receipt.status != 1:
        raise RuntimeError(f'the ETH transfer reverted: {h}')
    return h


def moveOnBase(predictor, switched: dict, walletKey: str, walletAddress: str) -> dict:
    """
    After the switch: the old alias's ETH to the new one, then, when the wallet
    was staked with the old alias, the stake moved to the new one (its fee set
    to 0 by the new alias, then the wallet's delegation). Each step on its own;
    a failure is logged and said (Stake on the dashboard does it again).
    """
    done = {}
    try:
        done['sweep'] = sweepEth(predictor.client, switched['oldKey'], switched['new']) or 'nothing to move'
    except Exception as e:
        done['sweep_error'] = str(e)
        logging.warning(f"could not move the old alias's ETH to the new one: {e}")
    try:
        if predictor.hub is not None and predictor.current_delegate(walletAddress).lower() == switched['old'].lower():
            done['stake'] = predictor.ensure_delegation(
                wallet_key=walletKey, wallet_address=walletAddress,
                alias_key=switched['newKey'], alias_address=switched['new'])
    except Exception as e:
        done['stake_error'] = str(e)
        logging.warning(f'could not move the stake to the new alias (Stake again on the dashboard): {e}')
    return done
