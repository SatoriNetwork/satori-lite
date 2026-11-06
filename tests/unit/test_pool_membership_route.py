"""The neuron sees the pool that lists it as a worker and can leave it: the stake
page's /api/pool/membership, passed through to Central's /pool/membership."""
from unittest import mock

import pytest


@pytest.fixture
def client():
    from web.app import create_app
    import web.routes as routes
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    with mock.patch.dict(routes._session_wallets, {'sid': mock.MagicMock()}):
        yield client


@pytest.mark.parametrize('method', ['GET', 'DELETE'])
def test_membership_passes_through_to_central(client, method):
    import web.routes as routes
    seen = {}

    def answer(*a, **k):
        seen['url'] = a[0]
        return mock.MagicMock(status_code=200, **{'json.return_value': {'pool_address': None}})
    with mock.patch.object(routes.requests, method.lower(), side_effect=answer):
        r = client.open('/api/pool/membership', method=method)
    assert r.status_code == 200
    assert seen['url'].endswith('/api/v1/pool/membership')


def test_the_stake_page_shows_the_pool_and_a_leave_button():
    page = open('web/templates/stake.html').read()
    assert "apiCall('/pool/membership')" in page
    assert "apiCall('/pool/membership', 'DELETE')" in page
    assert 'id="workerPoolLeaveBtn"' in page
