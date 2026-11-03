"""The alias derived from the wallet, as the Satori app derives it (init/derived_alias.py).

Unlocking the wallet switches an alias that is not the derived one: the new
file written and checked, the old kept as <file>.retired-<time>, the wallet's
link to the new alias; then on Base the old alias's ETH and a stake with it
move to the new alias.
"""
import pytest
import yaml

from satorineuron import config
from satorineuron.init import derived_alias
from satorineuron.init.derived_alias import aliasKeyFromWallet, addressOf, moveOnBase, switchToDerivedAlias
from satorineuron.init.wallet import WalletManager, readWalletLink
from satorilib.wallet.evrmore.identity import EvrmoreIdentity
from satorilib.wallet.link import linkIsFor

PASSWORD = 'wallet pass'

# the app's derived aliases for satori/test/fixtures/alias-parity.json's vectors (src/keys/derive.js keysFromWallet)
APP = [
    ('93173a8c1485c17add114d53ef1949a08aafc5fc2d2c0338adfa19a7f4a3292c', '5a7babc6a07d287f28646d9e685cb5c0e366a1b3f8a51c7f58587145d3898283', '0xB1cE5D8F769492D60d253eE90f99f8CE48cbdEaC'),
    ('efe1242ace1145295f58bfcd85384809337f644b487a2252cbfbe9bfa813bc58', '6e62fab80252c6a18e75bb6e9cce3c9cfef87ed9cfff5486acf2a42bdb4a9ed9', '0xD0a4D4C08A03A58abe9cf64978CC04743B2B6e96'),
    ('ea16cbf67fd735b40a0309aa6794b04dc3d0751174ddfa8d68a601971a40ab91', 'fac22e80036e500e0802279bf51842b15e7a3f042cef9aa3ff90f5dff9529a8d', '0xb65F5BA7570e59A00DDFf5B26fdc48f282074a42'),
    ('00d234bc65d8d093ccfceb0b525cc1c9f05e9d93189f268a0a26aa1152ddebea', 'fd6395a0a2cd8eed3a384676d2f58cd7f915e109a07850e9ce830aa1c4ed85af', '0x993C7174CD47afE85b31c86Fbd5653DFCdA856C3'),
]


@pytest.fixture
def keys(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'path', lambda of='data': str(tmp_path))
    return tmp_path


@pytest.mark.parametrize('entropy,key,address', APP)
def test_the_same_alias_as_the_app(entropy, key, address):
    k = aliasKeyFromWallet(bytes.fromhex(entropy))
    assert k.hex() == key
    assert addressOf(k) == address


def test_unlocking_switches_to_the_derived_alias_keeping_the_old_one(keys):
    manager = WalletManager.create(useConfigPassword=False)
    old = EvrmoreIdentity(str(keys / 'alias.yaml'))
    oldAddress = manager.alias.ethAddress
    manager.openWallet(password=PASSWORD, create=True)
    assert linkIsFor(readWalletLink(), oldAddress, manager.wallet.ethAddress)  # linked as it opened
    (keys / 'alias.scripts.json').write_text('{"channel": "the old alias\'s"}')

    switched = switchToDerivedAlias(manager)
    derived = addressOf(aliasKeyFromWallet(bytes(manager.wallet.identity._entropy)))
    assert switched['old'] == oldAddress and switched['new'] == derived
    assert manager.alias.ethAddress == derived
    assert WalletManager.create(useConfigPassword=False).alias.ethAddress == derived  # the next start reads it
    # the old alias kept, never deleted: its key in the retired file
    retired = [p for p in keys.iterdir() if p.name.startswith('alias.yaml.retired-')]
    assert [p.name for p in retired] == [switched['retired'].split('/')[-1]]
    assert yaml.safe_load(open(retired[0]))['entropy'] == old._entropyStr
    assert switched['oldKey'] == '0x' + bytes(old._entropy).hex()
    at = switched['retired'].split('-')[-1]
    assert (keys / f'alias.scripts.json.retired-{at}').read_text() == '{"channel": "the old alias\'s"}'  # set aside with it
    assert sorted(p.name for p in keys.iterdir() if '.retired-' not in p.name) == [
        'alias.yaml', 'alias.yaml.bak', 'wallet-link.yaml', 'wallet.yaml', 'wallet.yaml.bak']  # nothing of the switch's left over
    # the wallet links the new alias
    assert linkIsFor(readWalletLink(), derived, manager.wallet.ethAddress)
    # already derived: nothing more
    assert switchToDerivedAlias(manager) is None
    assert len([p for p in keys.iterdir() if p.name.startswith('alias.yaml.retired-')]) == 1


def test_a_locked_wallet_switches_nothing(keys):
    manager = WalletManager.create(useConfigPassword=False)
    assert switchToDerivedAlias(manager) is None
    manager.openWallet(password=PASSWORD, create=True)
    manager.closeWallet()
    assert switchToDerivedAlias(manager) is None


def test_the_older_layout_switches_its_own_alias_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'path', lambda of='data': str(tmp_path))
    EvrmoreIdentity(str(tmp_path / 'wallet.yaml'))  # an install from before: wallet.yaml is the alias
    manager = WalletManager.create(useConfigPassword=False)
    manager.openWallet(password=PASSWORD, create=True)
    assert (tmp_path / 'vault.yaml').exists()
    switched = switchToDerivedAlias(manager)
    assert switched['retired'].split('/')[-1].startswith('wallet.yaml.retired-')
    assert EvrmoreIdentity(str(tmp_path / 'wallet.yaml')).address == manager.alias.address


class FakePredictor:
    def __init__(self, delegate):
        self.hub = object()
        self.client = 'the client'
        self.delegate = delegate
        self.calls = []

    def current_delegate(self, address):
        return self.delegate

    def ensure_delegation(self, **kw):
        self.calls.append(kw)
        return {'delegate_tx': '0xd'}


SWITCHED = {'old': '0x' + '1' * 40, 'oldKey': '0xold', 'new': '0x' + '2' * 40, 'newKey': '0xnew', 'retired': 'x'}


def test_on_base_the_eth_then_a_stake_with_the_old_alias_move_to_the_new(monkeypatch):
    sent = []
    monkeypatch.setattr(derived_alias, 'sweepEth', lambda client, key, to: sent.append((client, key, to)) or '0xs')
    p = FakePredictor(SWITCHED['old'].upper().replace('0X', '0x'))
    assert moveOnBase(p, SWITCHED, '0xwallet', '0xW') == {'sweep': '0xs', 'stake': {'delegate_tx': '0xd'}}
    assert sent == [('the client', '0xold', SWITCHED['new'])]
    assert p.calls == [{'wallet_key': '0xwallet', 'wallet_address': '0xW', 'alias_key': '0xnew', 'alias_address': SWITCHED['new']}]
    # staked elsewhere, or not at all: left alone
    other = FakePredictor('0x' + '3' * 40)
    assert moveOnBase(other, SWITCHED, '0xwallet', '0xW') == {'sweep': '0xs'}
    assert other.calls == []


def test_on_base_each_step_on_its_own(monkeypatch):
    def broke(*a):
        raise RuntimeError('no gas')
    monkeypatch.setattr(derived_alias, 'sweepEth', broke)
    p = FakePredictor(SWITCHED['old'])
    assert moveOnBase(p, SWITCHED, '0xwallet', '0xW') == {'sweep_error': 'no gas', 'stake': {'delegate_tx': '0xd'}}
