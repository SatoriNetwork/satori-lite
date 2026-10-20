"""Merkle rewards on Base can land on the vault's address or on the identity's.

Central records a neuron's rewards at its reward address, else its vault's Base
address, else (no vault) its identity's. A reward address naming an identity
is paid at that identity's Base address. So the dashboard shows what is
claimable on both, and claims either one, signed by that key. A claim is sent
from the claiming address, so with no ETH there it says so instead of failing.
"""
from unittest import mock

import pytest

VAULT = '0x7ECAb9668d259057206b2DdfA0D786CD977e9345'
IDENTITY = '0x987560CEf66Df36EB5D4B4A11AaD0C46950Fc8a1'
VAULT_KEY = '0x' + '11' * 32
IDENTITY_KEY = '0x' + '22' * 32
CENTRAL = 'http://central.test'
WEI = 10 ** 18


class FakeClaimer:
    def __init__(self, minted=None, eth=None):
        self.minted = minted or {}
        self.eth = eth or {}
        self.satori = {}
        self.claims = []

    def token_balance(self, address):
        if isinstance(self.satori.get(address), Exception):
            raise self.satori[address]
        return self.satori.get(address, 0)

    def already_minted(self, address):
        return self.minted.get(address, 0)

    def eth_balance(self, address):
        return self.eth.get(address, 0)

    def claim(self, private_key, lifetime_entitlement, proof):
        self.claims.append((private_key, lifetime_entitlement, proof))
        return '0xtx'


def _key(hex_key):
    return mock.MagicMock(**{'account.key.to_0x_hex.return_value': hex_key})


@pytest.fixture
def web():
    """(client, set_drop, claimer): a logged-in dashboard; set_drop({address: wei})
    is central's published drop; claimer is the fake chain."""
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    app.config['SATORI_API_URL'] = CENTRAL
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['vault_open'] = True
        sess['session_id'] = 'sid'
    manager = mock.MagicMock()
    manager.vault = _key(VAULT_KEY)
    manager.vault.ethAddress = VAULT
    manager.vault.isDecrypted = True
    manager.wallet = _key(IDENTITY_KEY)
    manager.wallet.ethAddress = IDENTITY
    drop = {}

    def proof(url, timeout=None):
        address = url.rsplit('/', 1)[-1]
        if address not in drop:
            return mock.MagicMock(status_code=404)
        return mock.MagicMock(status_code=200, **{'json.return_value': {
            'amountWei': str(drop[address]), 'proof': ['0x' + 'ab' * 32]}})

    claimer = FakeClaimer()
    with mock.patch.dict(routes._session_vaults, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_vault', return_value=manager), \
            mock.patch.object(routes, '_base_claimer', return_value=claimer), \
            mock.patch.object(routes.requests, 'get', side_effect=proof):
        yield client, drop.update, claimer


def test_claimable_shows_vault_and_identity(web):
    client, set_drop, claimer = web
    set_drop({VAULT: 10 * WEI, IDENTITY: 3 * WEI})
    claimer.minted = {VAULT: 4 * WEI}
    r = client.get('/api/wallet/base/claimable').get_json()
    assert (r['address'], r['claimable'], r['total'], r['claimed']) == (VAULT, 6.0, 10.0, 4.0)
    identity = r['identity']
    assert (identity['address'], identity['claimable'], identity['total']) == (IDENTITY, 3.0, 3.0)
    assert identity['claimable_wei'] == str(3 * WEI)


def test_claimable_identity_not_in_drop(web):
    client, set_drop, _ = web
    set_drop({VAULT: WEI})
    r = client.get('/api/wallet/base/claimable').get_json()
    assert r['claimable'] == 1.0
    assert r['identity']['claimable'] == 0


def test_claim_identity_signs_with_identity(web):
    client, set_drop, claimer = web
    set_drop({IDENTITY: 3 * WEI})
    claimer.minted = {IDENTITY: WEI}
    claimer.eth = {IDENTITY: 1}
    r = client.post('/api/wallet/base/claim', json={'key': 'identity'})
    assert r.status_code == 200
    assert r.get_json()['claimed'] == 2.0
    assert claimer.claims == [(IDENTITY_KEY, 3 * WEI, ['0x' + 'ab' * 32])]


def test_claim_identity_without_eth_says_so(web):
    client, set_drop, claimer = web
    set_drop({IDENTITY: 3 * WEI})
    r = client.post('/api/wallet/base/claim', json={'key': 'identity'})
    assert r.status_code == 400
    error = r.get_json()['error']
    assert 'no ETH' in error and IDENTITY in error
    assert claimer.claims == []


def test_claim_defaults_to_vault(web):
    client, set_drop, claimer = web
    set_drop({VAULT: 2 * WEI, IDENTITY: 3 * WEI})
    claimer.eth = {VAULT: 1}
    r = client.post('/api/wallet/base/claim')
    assert r.status_code == 200
    assert claimer.claims == [(VAULT_KEY, 2 * WEI, ['0x' + 'ab' * 32])]


def test_balances_shows_vault_and_identity(web):
    client, _, claimer = web
    claimer.satori = {VAULT: 250 * WEI}
    claimer.eth = {VAULT: WEI // 100, IDENTITY: WEI // 20}
    r = client.get('/api/wallet/base/balances').get_json()
    assert (r['vault']['address'], r['vault']['satori'], r['vault']['eth']) == (VAULT, 250.0, 0.01)
    assert r['vault']['satori_wei'] == str(250 * WEI)
    assert (r['identity']['address'], r['identity']['satori'], r['identity']['eth']) == (IDENTITY, 0.0, 0.05)


def test_balances_one_failed_read_keeps_the_other(web):
    client, _, claimer = web
    claimer.satori = {VAULT: RuntimeError('rpc down'), IDENTITY: WEI}
    r = client.get('/api/wallet/base/balances')
    assert r.status_code == 200
    body = r.get_json()
    assert body['vault'] == {'address': VAULT, 'error': 'rpc down'}
    assert body['identity']['satori'] == 1.0
