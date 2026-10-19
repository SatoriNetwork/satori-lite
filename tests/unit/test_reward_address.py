"""The reward address is a Base address, and central is its source.

Setting one: a 0x address (any case, or correctly checksummed) is posted to
central checksummed and kept locally only once central accepts it; anything
else is refused with a plain reason, and so is central's 400.

Syncing (startup, and each time the dashboard reads it): the local copy mirrors
what central answers, which is the Base address rewards are paid at, or none.
An old Evrmore value kept locally is dropped, never re-posted.
"""
import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

_START = Path(__file__).parent.parent.parent / 'neuron-lite' / 'start.py'

# start.py pulls in the AI engine; nothing here needs it. Stub only that, and
# only while importing (patch.dict would also drop numpy & co., which can't be
# imported twice).
_engine = mock.MagicMock()
_stubs = {
    'satoriengine': _engine,
    'satoriengine.veda': _engine.veda,
    'satoriengine.veda.engine': _engine.veda.engine}
_stubbed = [name for name in _stubs if name not in sys.modules]
sys.modules.update({name: _stubs[name] for name in _stubbed})
try:
    _spec = importlib.util.spec_from_file_location('_start_reward_address', _START)
    start = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(start)
finally:
    for name in _stubbed:
        sys.modules.pop(name, None)

BASE = '0x2F66D2d1DfA3cbF191dd26394d3b241C90B57E74'
OTHER = '0x9f505692808622fc1F0fbb53324262162C788762'
EVRMORE = 'ERNue8bxEjwzQzEgWzcrD5CiXYk9NnKumL'


class FakeConfig:
    def __init__(self, data=None):
        self.data = dict(data or {})

    def get(self):
        return dict(self.data)

    def add(self, data=None):
        self.data.update(data or {})


@pytest.fixture
def neuron(monkeypatch):
    def make(local=None, central=''):
        cfg = FakeConfig({} if local is None else {'reward address': local})
        monkeypatch.setattr(start, 'config', cfg)
        n = object.__new__(start.StartupDag)
        n.env = 'prod'
        n.configRewardAddress = None
        n.server = mock.MagicMock()
        n.server.mineToAddressStatus.return_value = central
        n.server.setRewardAddress.return_value = (True, '{"reward_address": "%s"}' % BASE)
        return n, cfg
    return make


# Setting ###################################################################


@pytest.mark.parametrize('given', [BASE, BASE.lower(), '0x' + BASE[2:].upper(), '  ' + BASE + ' '])
def test_set_posts_checksummed_and_keeps_it(neuron, given):
    n, cfg = neuron()
    assert n.setRewardAddress(address=given, globally=True) == (True, '')
    n.server.setRewardAddress.assert_called_once_with(address=BASE)
    assert n.configRewardAddress == BASE
    assert cfg.data['reward address'] == BASE


@pytest.mark.parametrize('given', [
    EVRMORE,
    BASE[:-1] + 'e',                     # mixed case, wrong checksum
    BASE[:-1],                           # too short
    BASE[2:],                            # no 0x
    '',
])
def test_set_refuses_anything_but_a_base_address(neuron, given):
    n, cfg = neuron(local=OTHER)
    ok, why = n.setRewardAddress(address=given, globally=True)
    assert not ok and 'Base address' in why
    n.server.setRewardAddress.assert_not_called()
    assert cfg.data['reward address'] == OTHER


def test_set_surfaces_centrals_refusal(neuron):
    n, cfg = neuron(local=OTHER)
    n.server.setRewardAddress.return_value = (False, '{"detail":"Invalid Base address"}')
    assert n.setRewardAddress(address=BASE, globally=True) == (False, 'Invalid Base address')
    assert cfg.data['reward address'] == OTHER


def test_set_when_central_gives_no_reason(neuron):
    n, cfg = neuron()
    n.server.setRewardAddress.return_value = (False, '')
    ok, why = n.setRewardAddress(address=BASE, globally=True)
    assert not ok and why
    assert 'reward address' not in cfg.data


# Syncing ###################################################################


@pytest.mark.parametrize('local', [None, EVRMORE, OTHER, BASE])
def test_sync_mirrors_central(neuron, local):
    n, cfg = neuron(local=local, central=BASE)
    n.setRewardAddress(globally=True)
    assert n.configRewardAddress == BASE
    assert cfg.data['reward address'] == BASE
    n.server.setRewardAddress.assert_not_called()


@pytest.mark.parametrize('local', [EVRMORE, OTHER])
def test_sync_clears_when_central_has_none(neuron, local):
    n, cfg = neuron(local=local, central='')
    n.setRewardAddress(globally=True)
    assert n.configRewardAddress == ''
    assert cfg.data['reward address'] == ''
    n.server.setRewardAddress.assert_not_called()


def test_sync_central_unreachable_keeps_only_a_base_value(neuron):
    n, _ = neuron(local=EVRMORE, central=None)
    n.setRewardAddress(globally=True)
    assert n.configRewardAddress == ''
    n, _ = neuron(local=BASE.lower(), central=None)
    n.setRewardAddress(globally=True)
    assert n.configRewardAddress == BASE


# The web route ############################################################


@pytest.fixture
def web():
    from web.app import create_app
    import web.routes as routes
    client = create_app(testing=True).test_client()
    with client.session_transaction() as sess:
        sess['vault_open'] = True
        sess['session_id'] = 'sid'
    manager = mock.MagicMock()
    manager.vault.isDecrypted = True
    startup = mock.MagicMock(configRewardAddress=EVRMORE)
    with mock.patch.dict(routes._session_vaults, {'sid': manager}), \
            mock.patch.object(routes, 'get_startup', return_value=startup):
        yield client, startup


def test_route_get_shows_what_central_answers(web):
    client, startup = web

    def sync(*args, **kwargs):
        startup.configRewardAddress = BASE
        return True, ''
    startup.setRewardAddress.side_effect = sync
    assert client.get('/api/peer/reward-address').get_json() == {'reward_address': BASE}


def test_route_post_saves(web):
    client, startup = web

    def accept(address=None, globally=False):
        startup.configRewardAddress = BASE
        return True, ''
    startup.setRewardAddress.side_effect = accept
    r = client.post('/api/peer/reward-address', json={'reward_address': BASE.lower()})
    assert r.status_code == 200
    assert r.get_json() == {'success': True, 'reward_address': BASE}


def test_route_post_refused_is_a_plain_400(web):
    client, startup = web
    startup.setRewardAddress.return_value = (False, 'Invalid Base address')
    r = client.post('/api/peer/reward-address', json={'reward_address': EVRMORE})
    assert r.status_code == 400
    assert r.get_json() == {'error': 'Invalid Base address'}
