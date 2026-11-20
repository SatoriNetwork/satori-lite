"""The wallet's private key (its Base key, the 0x hex a wallet imports) comes
only with the wallet's password: /api/wallet/base gives the address, never the
key, so opening the dashboard sends no key over the network."""
from unittest import mock

import pytest

WALLET = '0x7ECAb9668d259057206b2DdfA0D786CD977e9345'
WALLET_KEY = '0x' + '11' * 32


@pytest.fixture
def client():
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
        sess['encrypted_wallet_password'] = routes.encrypt_wallet_password('pass1234')
    manager = mock.MagicMock()
    manager.wallet.ethAddress = WALLET
    manager.wallet.account.key.to_0x_hex.return_value = WALLET_KEY
    with mock.patch.dict(routes._session_wallets, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_wallet', return_value=manager), \
            mock.patch.object(routes, '_base_token_info', return_value={}):
        yield client


def test_the_wallet_card_gets_the_address_and_no_key(client):
    r = client.get('/api/wallet/base')
    assert r.status_code == 200
    assert r.get_json()['base_address'] == WALLET
    assert WALLET_KEY[2:] not in r.get_data(as_text=True)


def test_the_wallet_password_shows_the_wallet_key(client):
    r = client.post('/api/wallet/private-key', json={'password': 'pass1234'})
    assert r.status_code == 200
    assert r.get_json() == {'private_key': WALLET_KEY}


@pytest.mark.parametrize('body', [{'password': 'wrong'}, {}, None])
def test_without_the_wallet_password_it_is_refused(client, body):
    r = client.post('/api/wallet/private-key', json=body) if body is not None \
        else client.post('/api/wallet/private-key')
    assert r.status_code == 403
    assert WALLET_KEY[2:] not in r.get_data(as_text=True)


def test_a_get_shows_nothing(client):
    assert client.get('/api/wallet/private-key').status_code == 405
