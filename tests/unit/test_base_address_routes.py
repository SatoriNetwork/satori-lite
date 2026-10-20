"""Central keys every neuron, pool, lender and relay by its Base address.

The dashboard and stake page send central the addresses /api/wallet/address
gives, so those are the Base addresses of the identity and the vault.
"""
from unittest import mock

import pytest

VAULT = '0x7ECAb9668d259057206b2DdfA0D786CD977e9345'
IDENTITY = '0x987560CEf66Df36EB5D4B4A11AaD0C46950Fc8a1'
NOSTR = 'e5e6ce948344b1e0178f9ccf49fcb499b4b4f00fc09c7637535ee82fc7bf503d'


@pytest.fixture
def web():
    """(client, startup): a logged-in session with a vault and identity."""
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['vault_open'] = True
        sess['session_id'] = 'sid'
    manager = mock.MagicMock()
    manager.vault.address = 'EVaultEvrmoreAddressxxxxxxxxxxxxxx'
    manager.vault.ethAddress = VAULT
    manager.vault.isDecrypted = True
    manager.wallet.address = 'EIdentityEvrmoreAddressxxxxxxxxxxx'
    manager.wallet.ethAddress = IDENTITY
    manager.wallet.pubkey = '02' + '11' * 32
    startup = mock.MagicMock(nostrPubkey=NOSTR)
    with mock.patch.dict(routes._session_vaults, {'sid': manager}), \
            mock.patch.object(routes, 'get_or_create_session_vault', return_value=manager), \
            mock.patch.object(routes, 'get_startup', return_value=startup):
        yield client, startup


def test_wallet_address_is_base(web):
    client, _ = web
    r = client.get('/api/wallet/address').get_json()
    assert (r['wallet_address'], r['vault_address']) == (IDENTITY, VAULT)
