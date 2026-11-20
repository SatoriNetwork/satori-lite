"""The neuron's local relay: the files its embedded strfry runs on, and the
settings page that shows its state."""
import json
import os
import re
import subprocess
from unittest.mock import patch

from satorineuron.relay_manager import LocalRelayManager
from web import routes
from web.app import create_app


class DummyStartup:
    uiPort = 24601
    nostrPubkey = 'f89e11d67850764c3d7a4a2c3c81e0ec4b06aef83e74bce70df5c277d0547c74'

    class RelayStub:
        @staticmethod
        def status():
            return {
                'desired_mode': 'off',
                'running_mode': 'off',
                'running': False,
                'public_port': 7777,
                'private_port': 7171,
                'public_host': 'testnet.satorinet.io',
                'private_host': 'relay.testnet.satorinet.io',
                'last_event_at': '2026-03-24T11:00:00Z',
                'last_health_check_at': '2026-03-24T11:01:00Z',
                'containers': {
                    'public': {'strfry': {'status': 'stopped', 'ports': {}, 'health': None}},
                    'private': {'strfry': {'status': 'stopped', 'ports': {}, 'health': None}},
                },
                'port_conflicts': {'public': None, 'private': None},
            }

    localRelay = RelayStub()


class DummyWallet:
    isDecrypted = True


class DummyWalletManager:
    wallet = DummyWallet()


def test_public_strfry_conf_has_open_write():
    manager = LocalRelayManager(DummyStartup())
    conf = manager._render_strfry_conf('public', 'Relay', 'Desc')
    assert 'plugin = ""' in conf
    assert 'port = 7777' in conf


def test_private_relay_runs_the_policy_written_beside_its_conf(tmp_path, monkeypatch):
    """The private relay's conf names, as strfry's write policy plugin, the
    file the manager writes in the mode's folder; run as strfry runs it, it
    takes the owner's events and refuses a stranger's stream."""
    monkeypatch.setattr(LocalRelayManager, '_mode_dir', lambda self, mode: tmp_path / mode)
    LocalRelayManager(DummyStartup())._write_mode_assets('private')
    conf = (tmp_path / 'private' / 'strfry.conf').read_text()
    plugin = re.search(r'plugin = "(.+)"', conf).group(1)
    assert plugin == str(tmp_path / 'private' / 'write-policy.py')
    assert os.access(plugin, os.X_OK)

    def answer_to(pubkey):
        event = {'id': 'e1', 'pubkey': pubkey, 'kind': 34600, 'tags': [['d', 'a-stream']]}
        ran = subprocess.run(
            [plugin], input=json.dumps({'type': 'new', 'event': event, 'sourceType': 'IP4'}) + '\n',
            capture_output=True, text=True, timeout=30)
        return json.loads(ran.stdout)['action']

    assert answer_to(DummyStartup.nostrPubkey) == 'accept'
    assert answer_to('0' * 64) == 'reject'


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
    assert public.process_name.endswith('-public-embedded-strfry')
    assert private.legacy_strfry.endswith('-private-strfry')
    assert public.legacy_network != private.legacy_network


def test_docker_candidate_endpoints_respect_env_and_common_paths():
    manager = LocalRelayManager(DummyStartup())
    with patch.dict('os.environ', {
        'DOCKER_HOST': 'tcp://docker.example.internal:2375',
        'SATORI_DOCKER_SOCKET': '/custom/docker.sock',
        'SATORI_DOCKER_NPIPE': 'npipe:////./pipe/docker_engine',
    }, clear=False), patch(
        'os.path.exists',
        side_effect=lambda path: path in {
            '/custom/docker.sock',
            '/var/run/docker.sock',
        },
    ):
        endpoints = manager._docker_candidate_endpoints()
    assert endpoints[0] == 'tcp://docker.example.internal:2375'
    assert 'npipe:////./pipe/docker_engine' in endpoints
    assert 'unix:///custom/docker.sock' in endpoints
    assert 'unix:///var/run/docker.sock' in endpoints


def test_settings_page_shows_the_local_relay_as_its_manager_reports_it():
    """/settings shows a logged-in session the local relay's state from the
    manager's status: the ports, the advertised hosts and the URLs made of
    them, the last event and the last health check."""
    client = create_app(testing=True).test_client()
    with client.session_transaction() as sess:
        sess['session_id'] = 'test-session'
        sess['wallet_open'] = True
    with patch.dict(routes._session_wallets, {'test-session': DummyWalletManager()}), \
            patch.object(routes, 'get_startup', return_value=DummyStartup()):
        resp = client.get('/settings')
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    for shown in (
            f'id="relayNostrPubkey">{DummyStartup.nostrPubkey}<',
            'id="relayPublicPortInput" value="7777"',
            'id="relayPrivatePortInput" value="7171"',
            'id="relayPublicHostInput" value="testnet.satorinet.io"',
            'id="relayPrivateHostInput" value="relay.testnet.satorinet.io"',
            'id="relayPublicUrl">ws://testnet.satorinet.io:7777<',
            'id="relayPrivateUrl">ws://relay.testnet.satorinet.io:7171<',
            'id="relayLastEvent">2026-03-24T11:00:00Z<',
            'id="relayLastHealth">2026-03-24T11:01:00Z<'):
        assert shown in page
    assert 'Docker' not in page


def test_status_names_no_docker(tmp_path, monkeypatch):
    """The relay is a strfry process inside the neuron, so the status the
    settings page reads says nothing of Docker."""
    monkeypatch.setattr(LocalRelayManager, '_mode_dir', lambda self, mode: tmp_path / mode)
    status = LocalRelayManager(DummyStartup()).status()
    assert status['running'] is False
    assert [key for key in status if 'docker' in key] == []
