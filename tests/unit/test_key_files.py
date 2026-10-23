"""The alias's and the wallet's files, in both Docker layouts.

A new install writes alias.yaml (the alias: unencrypted, made at startup with
no input) and wallet.yaml (the wallet: encrypted with the password, made at
wallet setup). An install from before keeps wallet.yaml (the alias) and
vault.yaml (the wallet). config.keyFiles tells them apart only by alias.yaml.
A user's key file is never moved, renamed or deleted.
"""
import hashlib
import os

import pytest
import yaml

from satorineuron import config
from satorineuron.init import wallet as walletModule
from satorineuron.init.wallet import WalletManager, readWalletLink
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.wallet.link import linkFields, linkIsFor

PASSWORD = 'wallet pass'


@pytest.fixture
def keys(tmp_path, monkeypatch):
    """A keys folder for config to use."""
    monkeypatch.setattr(config, 'path', lambda of='data': str(tmp_path))
    return tmp_path


def _digest(path) -> str:
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def _pubkey(path) -> str:
    return yaml.safe_load(open(path))['publicKey']


def _manager() -> WalletManager:
    return WalletManager.create(useConfigPassword=False)


# config.keyFiles ##########################################################


def test_fresh_folder_is_the_newer_layout(keys):
    assert config.keyFiles() == {'alias': 'alias.yaml', 'wallet': 'wallet.yaml'}
    assert config.aliasPath() == str(keys / 'alias.yaml')
    assert config.walletPath() == str(keys / 'wallet.yaml')


def test_alias_yaml_means_the_newer_layout(keys):
    (keys / 'alias.yaml').write_text('x')
    (keys / 'wallet.yaml').write_text('x')
    assert config.keyFiles() == {'alias': 'alias.yaml', 'wallet': 'wallet.yaml'}


def test_wallet_yaml_without_alias_yaml_is_the_older_layout(keys):
    (keys / 'wallet.yaml').write_text('x')
    assert config.keyFiles() == {'alias': 'wallet.yaml', 'wallet': 'vault.yaml'}
    assert config.aliasPath() == str(keys / 'wallet.yaml')
    assert config.walletPath() == str(keys / 'vault.yaml')


def test_key_files_of_another_folder(tmp_path):
    (tmp_path / 'wallet.yaml').write_text('x')
    (tmp_path / 'vault.yaml').write_text('x')
    assert config.keyFiles(str(tmp_path)) == {'alias': 'wallet.yaml', 'wallet': 'vault.yaml'}


# a new install ############################################################


def test_new_install_makes_the_alias_at_startup_then_the_wallet_at_setup(keys):
    manager = _manager()
    assert (keys / 'alias.yaml').exists()
    assert not (keys / 'wallet.yaml').exists()
    assert manager.wallet is None
    assert manager.alias.pubkey == _pubkey(keys / 'alias.yaml')
    assert readWalletLink() is None  # no wallet, no link

    wallet = manager.openWallet(password=PASSWORD, create=True)
    assert wallet.isDecrypted
    assert (keys / 'wallet.yaml').exists()
    assert not (keys / 'vault.yaml').exists()
    assert EvrmoreIdentity(str(keys / 'wallet.yaml')).isEncrypted
    assert config.keyFiles() == {'alias': 'alias.yaml', 'wallet': 'wallet.yaml'}

    # the wallet linked the alias when it was decrypted at setup
    link = readWalletLink()
    assert linkIsFor(link, manager.alias.ethAddress, wallet.ethAddress)
    assert set(yaml.safe_load(open(keys / 'wallet-link.yaml'))) == {'message', 'signature'}


def test_new_install_reopens_both(keys):
    first = _manager()
    first.openWallet(password=PASSWORD, create=True)
    alias, wallet = first.alias.pubkey, first.wallet.pubkey

    again = _manager()
    assert again.alias.pubkey == alias
    assert again.wallet is not None and not again.wallet.isDecrypted
    assert again.openWallet(password=PASSWORD).pubkey == wallet


# an older install #########################################################


def _olderInstall(keys, withWallet: bool):
    EvrmoreIdentity(str(keys / 'wallet.yaml'))  # the alias, unencrypted
    if withWallet:
        EvrmoreIdentity(str(keys / 'vault.yaml'), password=PASSWORD)


def test_older_install_keeps_its_files(keys):
    _olderInstall(keys, withWallet=True)
    before = {name: _digest(keys / name) for name in ('wallet.yaml', 'vault.yaml')}

    manager = _manager()
    assert manager.alias.pubkey == _pubkey(keys / 'wallet.yaml')
    assert manager.wallet.pubkey == _pubkey(keys / 'vault.yaml')
    assert manager.openWallet(password=PASSWORD).isDecrypted

    assert not (keys / 'alias.yaml').exists()
    assert {name: _digest(keys / name) for name in before} == before
    assert config.keyFiles() == {'alias': 'wallet.yaml', 'wallet': 'vault.yaml'}


def test_older_install_without_a_wallet_sets_it_up_in_vault_yaml(keys):
    """wallet.yaml there is the alias: setting the wallet up later must not touch it."""
    _olderInstall(keys, withWallet=False)
    alias = _digest(keys / 'wallet.yaml')

    manager = _manager()
    assert manager.wallet is None
    assert manager.alias.pubkey == _pubkey(keys / 'wallet.yaml')

    wallet = manager.openWallet(password=PASSWORD, create=True)
    assert wallet.isDecrypted
    assert (keys / 'vault.yaml').exists()
    assert wallet.pubkey == _pubkey(keys / 'vault.yaml')
    assert _digest(keys / 'wallet.yaml') == alias
    assert not (keys / 'alias.yaml').exists()
    assert config.keyFiles() == {'alias': 'wallet.yaml', 'wallet': 'vault.yaml'}

    # and a later start finds both where they are
    again = _manager()
    assert again.alias.pubkey == manager.alias.pubkey
    assert again.wallet.pubkey == wallet.pubkey


def test_older_install_link_names_its_alias_and_wallet(keys):
    _olderInstall(keys, withWallet=True)
    manager = _manager()
    wallet = manager.openWallet(password=PASSWORD)
    fields = linkFields(readWalletLink()['message'])
    assert fields['alias'] == manager.alias.ethAddress
    assert fields['wallet'] == wallet.ethAddress


# the link #################################################################


def test_a_locked_wallet_makes_no_link(keys):
    _manager().openWallet(password=PASSWORD, create=True)
    os.remove(keys / 'wallet-link.yaml')
    manager = _manager()
    assert manager.ensureWalletLink() is None
    assert readWalletLink() is None


def test_a_wrong_password_makes_no_link(keys):
    _manager().openWallet(password=PASSWORD, create=True)
    os.remove(keys / 'wallet-link.yaml')
    manager = _manager()
    assert not manager.openWallet(password='not it').isDecrypted
    assert readWalletLink() is None


def test_unlock_keeps_a_link_that_is_still_right(keys, monkeypatch):
    manager = _manager()
    manager.openWallet(password=PASSWORD, create=True)
    link = readWalletLink()
    monkeypatch.setattr(walletModule, 'makeLink', lambda *a, **k: pytest.fail('signed again'))
    manager.openWallet(password=PASSWORD)
    assert readWalletLink() == link


def test_unlock_replaces_a_link_for_another_alias(keys):
    manager = _manager()
    manager.openWallet(password=PASSWORD, create=True)
    # another alias's link (e.g. the alias file was replaced by an import)
    other = '0x' + '11' * 20
    stale = {'message': readWalletLink()['message'].replace(
        manager.alias.ethAddress, other), 'signature': '0x00'}
    walletModule.writeWalletLink(stale)
    manager.openWallet(password=PASSWORD)
    assert linkIsFor(readWalletLink(), manager.alias.ethAddress, manager.wallet.ethAddress)
