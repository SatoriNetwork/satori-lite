"""Central keys every neuron, pool, lender and relay by its Base address.

The dashboard and stake page send central the addresses /api/wallet/address
gives, so those are the Base addresses of the alias and the wallet. The
settings relay card shows the Base address central pays this node's relay at
(its /peer/relays entry), and the key normalizer gives the Base address that
key controls. /api/lender/pay (an Evrmore send to central's addresses) is gone.
"""
from unittest import mock

import pytest

WALLET = '0x7ECAb9668d259057206b2DdfA0D786CD977e9345'
ALIAS = '0x987560CEf66Df36EB5D4B4A11AaD0C46950Fc8a1'
NOSTR = 'e5e6ce948344b1e0178f9ccf49fcb499b4b4f00fc09c7637535ee82fc7bf503d'
RELAY_BASE = '0xF9756ad42091d2d1ff75498c3017Bc964CefE16d'


@pytest.fixture
def web():
    """(client, startup): a logged-in session with a wallet and alias."""
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    manager = mock.MagicMock()
    manager.wallet.address = 'EWalletEvrmoreAddressxxxxxxxxxxxxxx'
    manager.wallet.ethAddress = WALLET
    manager.wallet.isDecrypted = True
    manager.alias.address = 'EAliasEvrmoreAddressxxxxxxxxxxx'
    manager.alias.ethAddress = ALIAS
    manager.alias.pubkey = '02' + '11' * 32
    startup = mock.MagicMock(nostrPubkey=NOSTR)
    with mock.patch.dict(routes._session_wallets, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_wallet', return_value=manager), \
            mock.patch.object(routes, 'get_startup', return_value=startup):
        yield client, startup


def test_wallet_address_is_base(web):
    client, _ = web
    r = client.get('/api/wallet/address').get_json()
    assert (r['alias_address'], r['wallet_address']) == (ALIAS, WALLET)


def test_relay_address_is_centrals_entry_for_our_nostr_key(web):
    client, startup = web
    startup.server.getRelays.return_value = [
        {'relay_url': 'wss://other', 'nostr_pubkey': 'ab' * 32, 'address': WALLET},
        {'relay_url': 'ws://ours', 'nostr_pubkey': NOSTR, 'address': RELAY_BASE}]
    r = client.get('/api/nostr/relay-address').get_json()
    assert r == {'nostr_pubkey': NOSTR, 'address': RELAY_BASE}


def test_relay_address_none_when_central_does_not_list_us(web):
    client, startup = web
    startup.server.getRelays.return_value = [
        {'relay_url': 'wss://other', 'nostr_pubkey': 'ab' * 32, 'address': WALLET}]
    r = client.get('/api/nostr/relay-address').get_json()
    assert r == {'nostr_pubkey': NOSTR, 'address': None}


def test_normalized_key_gives_its_base_address(web):
    from eth_account import Account
    client, _ = web
    secret = '7f' * 32
    r = client.post('/api/nostr/normalize-key', json={'secret': secret}).get_json()
    assert r['base_address'] == Account.from_key(r['secret_hex']).address


def test_lender_pay_is_gone(web):
    client, _ = web
    assert client.post('/api/lender/pay', json={'payments': {}}).status_code == 404
