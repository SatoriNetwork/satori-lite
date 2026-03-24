import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve()
REPO_ROOT = HERE.parents[1]
CANDIDATE_PATHS = [
    REPO_ROOT,
    REPO_ROOT / 'neuron-lite',
    REPO_ROOT.parent / 'satorilib' / 'src',
    Path('/Satori'),
    Path('/Satori/Neuron'),
    Path('/Satori/Lib'),
    Path('/Satori/Lib/satorilib/src'),
]

for candidate in CANDIDATE_PATHS:
    if candidate.exists():
        sys.path.insert(0, str(candidate))

from satorineuron.relay_manager import LocalRelayManager
from web.app import create_app
from web.routes import _session_vaults, set_startup


class DummyStartup:
    uiPort = 24601
    nostrPubkey = 'f89e11d67850764c3d7a4a2c3c81e0ec4b06aef83e74bce70df5c277d0547c74'

    class RelayStub:
        @staticmethod
        def status():
            return {
                'docker_available': False,
                'docker_error': 'not mounted in test',
                'docker_endpoint': None,
                'desired_mode': 'off',
                'running_mode': 'off',
                'running': False,
                'public_port': 7777,
                'private_port': 7171,
                'public_host': 'testnet.satorinet.io',
                'private_host': 'relay.testnet.satorinet.io',
                'last_event_at': '2026-03-24T11:00:00Z',
                'last_health_check_at': '2026-03-24T11:01:00Z',
                'containers': {},
            }

    localRelay = RelayStub()


class DummyVault:
    isDecrypted = True


class DummyWalletManager:
    vault = DummyVault()


def test_public_strfry_conf_has_open_write():
    manager = LocalRelayManager(DummyStartup())
    conf = manager._render_strfry_conf('public', 'Relay', 'Desc')
    assert 'plugin = ""' in conf
    assert 'port = 7777' in conf


def test_private_strfry_conf_uses_policy_plugin():
    manager = LocalRelayManager(DummyStartup())
    conf = manager._render_strfry_conf('private', 'Relay', 'Desc')
    assert 'plugin = "/app/write-policy.py"' in conf


def test_private_policy_contains_owner_pubkey_and_owned_stream_guard():
    manager = LocalRelayManager(DummyStartup())
    policy = manager._render_private_policy(DummyStartup.nostrPubkey)
    assert DummyStartup.nostrPubkey in policy
    assert 'foreign prediction for owned source stream' in policy
    assert 'foreign prediction does not target an owned source stream' in policy
    assert 'foreign pubkey may not publish non-prediction streams' in policy


def test_names_are_deterministic_per_mode():
    manager = LocalRelayManager(DummyStartup())
    public = manager._names('public')
    private = manager._names('private')
    assert public.nginx.endswith('-public-nginx')
    assert private.strfry.endswith('-private-strfry')
    assert public.network != private.network


def test_docker_candidate_endpoints_respect_env_and_common_paths():
    manager = LocalRelayManager(DummyStartup())
    with patch.dict('os.environ', {
        'DOCKER_HOST': 'tcp://docker.example.internal:2375',
        'SATORI_DOCKER_SOCKET': '/custom/docker.sock',
    }, clear=False), patch(
        'os.path.exists',
        side_effect=lambda path: path in {
            '/custom/docker.sock',
            '/var/run/docker.sock',
        },
    ):
        endpoints = manager._docker_candidate_endpoints()
    assert endpoints[0] == 'tcp://docker.example.internal:2375'
    assert 'unix:///custom/docker.sock' in endpoints
    assert 'unix:///var/run/docker.sock' in endpoints


def test_settings_page_renders_for_logged_in_session():
    set_startup(DummyStartup())
    app = create_app(testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['session_id'] = 'test-session'
        sess['vault_open'] = True
    _session_vaults['test-session'] = DummyWalletManager()
    try:
        resp = client.get('/settings')
        assert resp.status_code == 200
        assert b'Nostr Relay Settings' in resp.data
        assert b'Public Relay' in resp.data
        assert b'Private Relay' in resp.data
        assert b'Public Relay Port' in resp.data
        assert b'Private Relay Port' in resp.data
        assert b'Relay Off' in resp.data
        assert b'Change before starting public relay' in resp.data
        assert b'Change before starting private relay' in resp.data
        assert b'Advertised Host / Domain' in resp.data
        assert b'Last Relay Event' in resp.data
        assert b'Last Health Check' in resp.data
    finally:
        _session_vaults.pop('test-session', None)
