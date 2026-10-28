"""Notifications from central: the neuron passes its version, marks which ones are
read (read ids live in the neuron config), caches the list, and shows nothing
rather than failing when central is down."""
from unittest import mock

import pytest

NOTES = [
    {'id': 2, 'message': 'newer', 'level': 'warning', 'link_url': '/stake', 'link_label': 'Go to Stake',
     'starts_at': '2026-10-28T00:00:00Z', 'expires_at': None},
    {'id': 1, 'message': 'older', 'level': 'info', 'link_url': None, 'link_label': None,
     'starts_at': '2026-10-20T00:00:00Z', 'expires_at': None},
]


@pytest.fixture
def web():
    """(client, central, cfg): a logged-in neuron UI; central is the mocked
    requests.get; cfg is the neuron config as a dict."""
    from web.app import create_app
    import web.routes as routes
    from satorineuron import config
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    cfg = {}
    central = mock.MagicMock(return_value=mock.MagicMock(status_code=200, **{'json.return_value': NOTES}))
    manager = mock.MagicMock()
    manager.wallet.isDecrypted = True
    with mock.patch.dict(routes._session_wallets, {'sid': manager}), \
            mock.patch.object(routes.requests, 'get', central), \
            mock.patch.object(config, 'get', side_effect=lambda *a, **k: dict(cfg)), \
            mock.patch.object(config, 'add', side_effect=lambda *a, data=None, **k: cfg.update(data or {})):
        yield client, central, cfg


def test_lists_with_version_and_read_flags(web):
    client, central, cfg = web
    from satorineuron import VERSION
    cfg['notifications read'] = [1]
    body = client.get('/api/notifications').get_json()
    assert [(n['id'], n['read']) for n in body['notifications']] == [(2, False), (1, True)]
    assert central.call_args.kwargs['params'] == {'version': VERSION}
    assert central.call_args.args[0].endswith('/api/v1/notifications')


def test_mark_read_saves_ids(web):
    client, _, cfg = web
    assert client.post('/api/notifications/read', json={'ids': [2, 'x', 2]}).get_json()['success']
    assert cfg['notifications read'] == [2]
    client.post('/api/notifications/read', json={'ids': [1]})
    assert cfg['notifications read'] == [1, 2]
    body = client.get('/api/notifications').get_json()
    assert all(n['read'] for n in body['notifications'])


def test_cached_between_calls(web):
    client, central, _ = web
    client.get('/api/notifications')
    client.get('/api/notifications')
    assert central.call_count == 1


def test_central_down_shows_nothing(web):
    client, central, _ = web
    central.side_effect = ConnectionError('down')
    assert client.get('/api/notifications').get_json() == {'notifications': []}


def test_needs_login():
    from web.app import create_app
    client = create_app(testing=True).test_client()
    r = client.get('/api/notifications')
    assert r.status_code in (302, 401)
