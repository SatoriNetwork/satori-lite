"""Merkle rewards on Base can land on the wallet's address or on the alias's.

Central records a neuron's rewards at its reward address, else its wallet's Base
address, else (no wallet) its alias's. A reward address naming an alias
is paid at that alias's Base address. So the app shows what is claimable
on both (the alias's in Settings), and claims either one, signed by that key. A claim is sent
from the claiming address, so with no ETH there it says so instead of failing.
"""
from unittest import mock

import pytest

WALLET = '0x7ECAb9668d259057206b2DdfA0D786CD977e9345'
ALIAS = '0x987560CEf66Df36EB5D4B4A11AaD0C46950Fc8a1'
WALLET_KEY = '0x' + '11' * 32
ALIAS_KEY = '0x' + '22' * 32
CENTRAL = 'http://central.test'
WEI = 10 ** 18


class FakeClaimer:
    def __init__(self, minted=None, eth=None):
        self.minted = minted or {}
        self.eth = eth or {}
        self.satori = {}
        self.claims = []
        self.airdrop = {}
        self.airdrop_claimed = {}
        self.airdrop_claims = []

    def token_balance(self, address):
        if isinstance(self.satori.get(address), Exception):
            raise self.satori[address]
        return self.satori.get(address, 0)

    def already_minted(self, address):
        return self.minted.get(address, 0)

    def eth_balance(self, address):
        return self.eth.get(address, 0)

    def airdrop_status(self, address):
        alloc = self.airdrop.get(address, 0)
        claimed = self.airdrop_claimed.get(address, 0)
        return {'allocation': alloc, 'claimed': claimed, 'claimable': alloc - claimed}

    def claim_airdrop(self, private_key):
        self.airdrop_claims.append(private_key)
        return '0xairdrop'

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
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    manager = mock.MagicMock()
    manager.wallet = _key(WALLET_KEY)
    manager.wallet.ethAddress = WALLET
    manager.wallet.isDecrypted = True
    manager.alias = _key(ALIAS_KEY)
    manager.alias.ethAddress = ALIAS
    drop = {}

    def proof(url, timeout=None):
        address = url.rsplit('/', 1)[-1]
        if address not in drop:
            return mock.MagicMock(status_code=404)
        return mock.MagicMock(status_code=200, **{'json.return_value': {
            'amountWei': str(drop[address]), 'proof': ['0x' + 'ab' * 32]}})

    claimer = FakeClaimer()
    with mock.patch.dict(routes._session_wallets, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_wallet', return_value=manager), \
            mock.patch.object(routes, '_base_claimer', return_value=claimer), \
            mock.patch.object(routes.requests, 'get', side_effect=proof):
        yield client, drop.update, claimer


def test_claimable_shows_wallet_and_alias(web):
    client, set_drop, claimer = web
    set_drop({WALLET: 10 * WEI, ALIAS: 3 * WEI})
    claimer.minted = {WALLET: 4 * WEI}
    r = client.get('/api/wallet/base/claimable').get_json()
    assert (r['address'], r['claimable'], r['total'], r['claimed']) == (WALLET, 6.0, 10.0, 4.0)
    alias = r['alias']
    assert (alias['address'], alias['claimable'], alias['total']) == (ALIAS, 3.0, 3.0)
    assert alias['claimable_wei'] == str(3 * WEI)


def test_claimable_alias_not_in_drop(web):
    client, set_drop, _ = web
    set_drop({WALLET: WEI})
    r = client.get('/api/wallet/base/claimable').get_json()
    assert r['claimable'] == 1.0
    assert r['alias']['claimable'] == 0


def test_claim_alias_signs_with_alias(web):
    client, set_drop, claimer = web
    set_drop({ALIAS: 3 * WEI})
    claimer.minted = {ALIAS: WEI}
    claimer.eth = {ALIAS: 1}
    r = client.post('/api/wallet/base/claim', json={'key': 'alias'})
    assert r.status_code == 200
    assert r.get_json()['claimed'] == 2.0
    assert claimer.claims == [(ALIAS_KEY, 3 * WEI, ['0x' + 'ab' * 32])]


def test_claim_alias_without_eth_says_so(web):
    client, set_drop, claimer = web
    set_drop({ALIAS: 3 * WEI})
    r = client.post('/api/wallet/base/claim', json={'key': 'alias'})
    assert r.status_code == 400
    error = r.get_json()['error']
    assert 'no ETH' in error and ALIAS in error
    assert claimer.claims == []


def test_claim_defaults_to_wallet(web):
    client, set_drop, claimer = web
    set_drop({WALLET: 2 * WEI, ALIAS: 3 * WEI})
    claimer.eth = {WALLET: 1}
    r = client.post('/api/wallet/base/claim')
    assert r.status_code == 200
    assert claimer.claims == [(WALLET_KEY, 2 * WEI, ['0x' + 'ab' * 32])]


def test_balances_shows_wallet_and_alias(web):
    client, _, claimer = web
    claimer.satori = {WALLET: 250 * WEI}
    claimer.eth = {WALLET: WEI // 100, ALIAS: WEI // 20}
    r = client.get('/api/wallet/base/balances').get_json()
    assert (r['wallet']['address'], r['wallet']['satori'], r['wallet']['eth']) == (WALLET, 250.0, 0.01)
    assert r['wallet']['satori_wei'] == str(250 * WEI)
    assert (r['alias']['address'], r['alias']['satori'], r['alias']['eth']) == (ALIAS, 0.0, 0.05)


def test_balances_one_failed_read_keeps_the_other(web):
    client, _, claimer = web
    claimer.satori = {WALLET: RuntimeError('rpc down'), ALIAS: WEI}
    r = client.get('/api/wallet/base/balances')
    assert r.status_code == 200
    body = r.get_json()
    assert body['wallet'] == {'address': WALLET, 'error': 'rpc down'}
    assert body['alias']['satori'] == 1.0


def test_airdrop_shows_the_wallet_and_the_alias(web):
    client, _, claimer = web
    claimer.airdrop = {WALLET: 18 * WEI // 10, ALIAS: WEI // 20}
    r = client.get('/api/wallet/base/airdrop').get_json()
    assert (r['address'], r['allocation'], r['claimable']) == (WALLET, 1.8, 1.8)
    assert (r['alias']['address'], r['alias']['claimable']) == (ALIAS, 0.05)


def test_alias_airdrop_is_claimed_with_the_alias_key(web):
    client, _, claimer = web
    claimer.airdrop = {ALIAS: WEI}
    claimer.eth = {ALIAS: WEI // 100}
    r = client.post('/api/wallet/base/airdrop/claim', json={'key': 'alias'}).get_json()
    assert r['success'] and r['claimed'] == 1.0
    assert claimer.airdrop_claims == [ALIAS_KEY]


def test_airdrop_claim_without_gas_says_so(web):
    client, _, claimer = web
    claimer.airdrop = {WALLET: WEI}
    response = client.post('/api/wallet/base/airdrop/claim', json={})
    assert response.status_code == 400
    assert 'no ETH' in response.get_json()['error'] and WALLET in response.get_json()['error']
    assert claimer.airdrop_claims == []


def test_airdrop_claim_with_nothing_owed_is_refused(web):
    client, _, claimer = web
    claimer.eth = {WALLET: WEI}
    response = client.post('/api/wallet/base/airdrop/claim', json={})
    assert response.status_code == 400
    assert 'Nothing to claim' in response.get_json()['error']
