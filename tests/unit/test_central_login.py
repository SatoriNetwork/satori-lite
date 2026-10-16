"""The web UI's JWT login to central signs EIP-191 (Ethereum personal_sign).

Central login takes headers wallet-pubkey (compressed secp256k1 hex), message
and signature. The signature is "0x" + r || s || v (v 27/28) by the identity
key, the same way satorilib's SatoriServerClient signs; central recovers the
signer and compares it with the Ethereum address of wallet-pubkey. It needs a
central with EIP-191 login (its base-upgrade branch).
"""
import re
from unittest.mock import MagicMock, patch

import pytest
from eth_account import Account
from eth_account.messages import encode_defunct

CENTRAL = 'http://central.test'


@pytest.fixture
def app():
    from web.app import create_app
    app = create_app(testing=True)
    app.config['SECRET_KEY'] = 'test-secret-key'
    app.config['SATORI_API_URL'] = CENTRAL
    return app


def _response(body):
    response = MagicMock(status_code=200)
    response.json.return_value = body
    return response


@pytest.mark.unit
def test_web_login_signs_eip191(app, test_wallet):
    import web.routes as routes
    from satorilib.wallet.evrmore.wallet import EvrmoreWallet

    wallet = EvrmoreWallet(identity=test_wallet, skipSave=True)
    manager = MagicMock(wallet=wallet)
    manager.vault.isDecrypted = True
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['vault_open'] = True
        sess['session_id'] = 'sid'

    def refuse(*args, **kwargs):
        raise AssertionError('Evrmore signing used for central login')

    login = _response({'access_token': 'access', 'refresh_token': 'refresh', 'expires_in': 3600})
    with patch.dict(routes._session_vaults, {'sid': manager}), \
            patch('satorilib.wallet.evrmore.identity.signMessage', refuse), \
            patch('satorilib.wallet.evrmore.wallet.signMessage', refuse), \
            patch.object(routes.requests, 'post', return_value=login) as post, \
            patch.object(routes.requests, 'get', return_value=_response({'balance': 1})) as get:
        response = client.get('/api/balance/get')

    assert response.status_code == 200
    (url,), kwargs = post.call_args
    headers = kwargs['headers']
    assert url == CENTRAL + '/api/v1/auth/login'
    assert set(headers) == {'wallet-pubkey', 'message', 'signature'}
    assert headers['wallet-pubkey'] == wallet.pubkey
    assert re.fullmatch(r'0x[0-9a-f]{130}', headers['signature'])
    assert bytes.fromhex(headers['signature'][2:])[64] in (27, 28)
    assert Account.recover_message(
        encode_defunct(text=headers['message']),
        signature=headers['signature']) == wallet.ethAddress
    assert get.call_args.kwargs['headers'] == {'Authorization': 'Bearer access'}
