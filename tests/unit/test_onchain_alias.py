"""Another alias for the Satori contract (satorineuron/init/onchain_alias.py):
kept in onchain-alias.yaml, used for staking and the daily batch, registered
for on-chain scoring while alias.yaml registers for off-chain."""
import os
from unittest.mock import MagicMock, patch

import pytest
import yaml
from eth_account import Account

from satorineuron.init import onchain_alias as oa
from satorineuron.base_predict import evm_address_from_pubkey

KEY = '0x' + '33' * 32
OTHER = '0x' + '44' * 32
OWN = Account.from_key('0x' + '55' * 32).address
WALLET = Account.from_key('0x' + '66' * 32).address


@pytest.fixture
def app():
    from web.app import create_app
    app = create_app(testing=True)
    app.config.update(TESTING=True, SECRET_KEY='test-secret-key')
    return app


def test_a_key_alias_has_the_address_and_compressed_pubkey_of_its_key():
    a = oa.KeyAlias(KEY)
    assert a.ethAddress == Account.from_key(KEY).address
    assert len(a.pubkey) == 66 and a.pubkey[:2] in ('02', '03')
    assert evm_address_from_pubkey(a.pubkey).lower() == a.ethAddress.lower()
    assert a.account.key.to_0x_hex() == KEY


def test_a_key_alias_signs_eip191_as_the_alias_does():
    """0x and r || s || v, recovering to its address: the register's proof by it."""
    from eth_account.messages import encode_defunct
    a = oa.KeyAlias(KEY)
    signature = a.ethSign('a challenge')
    assert signature.startswith('0x') and len(signature) == 132
    assert Account.recover_message(encode_defunct(text='a challenge'), signature=signature) == a.ethAddress


def test_set_stores_it_and_a_replaced_or_handed_back_one_is_kept_renamed(tmp_path):
    path = str(tmp_path / 'onchain-alias.yaml')
    assert oa.readOnchainAlias(path) is None
    a = oa.setOnchainAlias(KEY[2:].upper(), own_alias=OWN, wallet=WALLET, path=path)
    assert a.ethAddress == Account.from_key(KEY).address
    assert oa.readOnchainAlias(path).ethAddress == a.ethAddress
    assert oct(os.stat(path).st_mode & 0o777) == '0o600'
    b = oa.setOnchainAlias(OTHER, own_alias=OWN, wallet=WALLET, path=path)
    assert oa.readOnchainAlias(path).ethAddress == b.ethAddress
    assert oa.setOnchainAlias('', own_alias=OWN, wallet=WALLET, path=path) is None
    assert oa.readOnchainAlias(path) is None
    kept = sorted(yaml.safe_load(open(tmp_path / f))['privateKey'] for f in os.listdir(tmp_path) if '.retired-' in f)
    assert kept == sorted([KEY, OTHER])  # keys are never deleted


def test_refused_the_wallet_its_current_one_no_key_and_nothing_to_hand_back(tmp_path):
    path = str(tmp_path / 'onchain-alias.yaml')
    with pytest.raises(ValueError, match="wallet's own key"):
        oa.setOnchainAlias('0x' + '66' * 32, own_alias=OWN, wallet=WALLET, path=path)
    with pytest.raises(ValueError, match='private key'):
        oa.setOnchainAlias('not a key', own_alias=OWN, wallet=WALLET, path=path)
    with pytest.raises(ValueError, match='already uses its own alias'):
        oa.setOnchainAlias('', own_alias=OWN, wallet=WALLET, path=path)
    with pytest.raises(ValueError, match='already uses its own alias'):  # the neuron's own: the job is its already
        oa.setOnchainAlias('0x' + '55' * 32, own_alias=OWN, wallet=WALLET, path=path)
    oa.setOnchainAlias(KEY, own_alias=OWN, wallet=WALLET, path=path)
    with pytest.raises(ValueError, match='uses for the Satori contract now'):
        oa.setOnchainAlias(KEY, own_alias=OWN, wallet=WALLET, path=path)


def test_the_wallet_manager_gives_the_onchain_alias_else_the_alias(tmp_path):
    from satorineuron.init.wallet import WalletManager
    manager = WalletManager.__new__(WalletManager)
    manager._alias = MagicMock(ethAddress=OWN)
    path = str(tmp_path / 'onchain-alias.yaml')
    with patch.object(oa, 'onchainAliasPath', return_value=path):
        assert manager.onchainAlias is manager._alias
        oa.setOnchainAlias(KEY, own_alias=OWN, wallet=WALLET, path=path)
        assert manager.onchainAlias.ethAddress == Account.from_key(KEY).address


class TestRegisterEachForItsJob:
    def _register(self, app, manager):
        import web.routes as routes
        answer = MagicMock(status_code=200)
        answer.json.return_value = {'success': True, 'peer_id': 7, 'wallet_address': WALLET}
        challenges = []
        for c in ('c1', 'c2'):
            challenges.append(MagicMock(status_code=200))
            challenges[-1].json.return_value = {'challenge': c}
        with app.test_request_context(), \
                patch.object(routes, 'get_startup', return_value=MagicMock(nostrPubkey='ab' * 32)), \
                patch.object(routes.requests, 'get', side_effect=challenges), \
                patch.object(routes.requests, 'post', return_value=answer) as post:
            result = routes.ensure_peer_registered(app, manager, max_retries=1)
        return result, [c.kwargs['headers'] for c in post.call_args_list]

    @staticmethod
    def _manager(onchain):
        manager = MagicMock()
        manager.alias.pubkey = '02' + '11' * 32
        manager.alias.ethSign = lambda message: f'signed:{message}'
        manager.ensureWalletLink.return_value = {'message': 'link alias', 'signature': '0xaa'}
        manager.onchainAlias = onchain
        manager.ensureOnchainLink.return_value = {'message': 'link onchain', 'signature': '0xbb'}
        return manager

    @pytest.mark.unit
    def test_one_alias_registers_once_for_both(self, app):
        manager = self._manager(None)
        manager.onchainAlias = manager.alias
        _, headers = self._register(app, manager)
        assert [(h['alias-pubkey'], h['alias-scoring']) for h in headers] == [('02' + '11' * 32, 'offchain,onchain')]

    @pytest.mark.unit
    def test_another_onchain_alias_registers_on_chain_with_its_own_link(self, app):
        import base64
        onchain = oa.KeyAlias(KEY)
        result, headers = self._register(app, self._manager(onchain))
        assert [(h['alias-pubkey'], h['alias-scoring'], h['alias-reported-by']) for h in headers] == [
            ('02' + '11' * 32, 'offchain', 'neuron'), (onchain.pubkey, 'onchain', 'neuron')]
        assert [base64.b64decode(h['wallet-link']).decode() for h in headers] == ['link alias', 'link onchain']
        assert result['onchain']['peer_id'] == 7

    @pytest.mark.unit
    def test_each_alias_proves_itself_with_its_own_challenge(self, app):
        """The on-chain register's proof is the on-chain key's signature of a
        challenge of its own, not the alias's."""
        from eth_account.messages import encode_defunct
        onchain = oa.KeyAlias(KEY)
        _, headers = self._register(app, self._manager(onchain))
        assert (headers[0]['message'], headers[0]['signature']) == ('c1', 'signed:c1')
        assert headers[1]['message'] == 'c2'
        assert Account.recover_message(
            encode_defunct(text='c2'), signature=headers[1]['signature']) == onchain.ethAddress
