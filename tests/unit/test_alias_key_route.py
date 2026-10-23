"""The alias's private key, for the rare case funds were sent to it: shown in
the form a Base wallet imports (0x hex), only with the wallet's password."""
from unittest import mock

import pytest

ALIAS_KEY = '0x' + '22' * 32


@pytest.fixture
def client():
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    client = app.test_client()
    encrypted, session_key = routes.encrypt_wallet_password('pass1234')
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
        sess['encrypted_wallet_password'] = encrypted
        sess['session_key'] = session_key
    manager = mock.MagicMock()
    manager.alias = mock.MagicMock(**{'account.key.to_0x_hex.return_value': ALIAS_KEY})
    with mock.patch.dict(routes._session_wallets, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_wallet', return_value=manager):
        yield client


def test_the_wallet_password_shows_the_alias_key_as_base_hex(client):
    r = client.post('/api/wallet/alias-private-key', json={'password': 'pass1234'})
    assert r.status_code == 200
    assert r.get_json() == {'private_key': ALIAS_KEY}


@pytest.mark.parametrize('body', [{'password': 'wrong'}, {}, None])
def test_without_the_wallet_password_it_is_refused(client, body):
    r = client.post('/api/wallet/alias-private-key', json=body) if body is not None \
        else client.post('/api/wallet/alias-private-key')
    assert r.status_code == 403
    assert 'private_key' not in (r.get_json() or {})


def test_a_get_shows_nothing(client):
    assert client.get('/api/wallet/alias-private-key').status_code == 405
