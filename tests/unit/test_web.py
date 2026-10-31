"""
Unit tests for neuron web UI.

Tests the Flask application, routes, and templates.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch, Mock


@pytest.fixture
def app():
    """Create Flask app for testing."""
    from web.app import create_app

    app = create_app(testing=True)
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['SECRET_KEY'] = 'test-secret-key'

    return app


@pytest.fixture
def client(app):
    """Create test client."""
    return app.test_client()


class TestAppCreation:
    """Test Flask app creation."""

    @pytest.mark.unit
    def test_app_creates_successfully(self, app):
        """App should be created without errors."""
        assert app is not None

    @pytest.mark.unit
    def test_app_has_secret_key(self, app):
        """App should have a secret key configured."""
        assert app.config['SECRET_KEY'] is not None


class TestLoginPage:
    """Test login/wallet unlock page."""

    @pytest.mark.unit
    def test_login_page_renders(self, client):
        """Login page should render successfully."""
        with patch('web.routes.check_wallet_file_exists', return_value=True):
            response = client.get('/login')
        assert response.status_code == 200
        assert b'password' in response.data.lower()

    @pytest.mark.unit
    def test_login_page_has_form(self, client):
        """Login page should contain a password form."""
        with patch('web.routes.check_wallet_file_exists', return_value=True):
            response = client.get('/login')
        assert b'<form' in response.data
        assert b'type="password"' in response.data

    @pytest.mark.unit
    def test_root_redirects_to_login_when_wallet_locked(self, client):
        """Root URL should redirect to login when wallet is locked."""
        with patch('web.routes.check_wallet_file_exists', return_value=True):
            response = client.get('/')
        assert response.status_code in [302, 303]
        assert b'/login' in response.data or '/login' in response.location


class TestWalletUnlock:
    """Test wallet unlock functionality."""

    @pytest.mark.unit
    def test_login_with_correct_password_redirects_to_dashboard(self, client):
        """Correct password should unlock wallet and redirect to dashboard."""
        mock_wallet_manager = MagicMock()
        mock_wallet = MagicMock()
        mock_wallet.isDecrypted = True
        mock_wallet_manager.openWallet.return_value = mock_wallet

        with patch('web.routes.check_wallet_file_exists', return_value=True):
            with patch('web.routes.get_or_create_session_wallet', return_value=mock_wallet_manager):
                response = client.post('/login', data={'password': 'correct_password'})

        assert response.status_code in [302, 303]
        assert '/dashboard' in response.location or b'/dashboard' in response.data

    @pytest.mark.unit
    def test_login_with_incorrect_password_shows_error(self, client):
        """Incorrect password should show error message."""
        mock_wallet_manager = MagicMock()
        mock_wallet_manager.openWallet.side_effect = Exception("Invalid password")

        with patch('web.routes.check_wallet_file_exists', return_value=True):
            with patch('web.routes.get_or_create_session_wallet', return_value=mock_wallet_manager):
                response = client.post('/login', data={'password': 'wrong_password'})

        assert response.status_code == 200
        assert b'error' in response.data.lower() or b'invalid' in response.data.lower()


class TestDashboard:
    """Test dashboard page."""

    @pytest.mark.unit
    def test_dashboard_requires_login(self, client):
        """Dashboard should redirect to login when not authenticated."""
        with patch('web.routes.check_wallet_file_exists', return_value=True):
            response = client.get('/dashboard')
        assert response.status_code in [302, 303]
        assert '/login' in response.location or b'/login' in response.data

    @pytest.mark.unit
    def test_dashboard_renders_when_logged_in(self, client):
        """Dashboard should render when wallet is open."""
        mock_wallet_manager = MagicMock()
        mock_wallet_manager.wallet = MagicMock(isDecrypted=True)

        # Simulate logged in session
        with client.session_transaction() as sess:
            sess['wallet_open'] = True
            sess['session_id'] = 'test-session-id'

        with patch.dict('web.routes._session_wallets', {'test-session-id': mock_wallet_manager}), \
                patch('web.routes.get_or_create_session_wallet', return_value=mock_wallet_manager):
            response = client.get('/dashboard')

        assert response.status_code == 200
        assert b'dashboard' in response.data.lower()


class TestLogout:
    """Test logout functionality."""

    @pytest.mark.unit
    def test_logout_clears_session(self, client):
        """Logout should clear session and redirect to login."""
        # Simulate logged in session
        with client.session_transaction() as sess:
            sess['wallet_open'] = True
            sess['session_id'] = 'test-session-id'

        with patch('web.routes.cleanup_session_wallet') as mock_cleanup:
            response = client.get('/logout')

        assert response.status_code in [302, 303]
        assert '/login' in response.location or b'/login' in response.data
        mock_cleanup.assert_called_once_with('test-session-id')


class TestHealthCheck:
    """Test health check endpoint."""

    @pytest.mark.unit
    def test_health_endpoint_returns_ok(self, client):
        """Health endpoint should return status ok."""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'


# The Satori Network register: the current form ############################

ALIAS_PUBKEY = '02' + '11' * 32
NOSTR_PUBKEY = 'ab' * 32


def _register_answer(body):
    response = MagicMock(status_code=200)
    response.json.return_value = body
    return response


def _manager(link=None):
    manager = MagicMock()
    manager.alias.pubkey = ALIAS_PUBKEY
    manager.ensureWalletLink.return_value = link
    manager.walletLink = link
    return manager


class TestEnsurePeerRegistered:
    """Register sends alias-pubkey, nostr-pubkey, neuron-version and the
    wallet's link when there is one; nothing named for the older roles."""

    def _register(self, app, manager, answer):
        import web.routes as routes
        startup = MagicMock(nostrPubkey=NOSTR_PUBKEY)
        with app.test_request_context(), \
                patch.object(routes, 'get_startup', return_value=startup), \
                patch.object(routes.requests, 'post', return_value=_register_answer(answer)) as post, \
                patch.object(routes.requests, 'get') as get:
            result = routes.ensure_peer_registered(app, manager, max_retries=1)
        return result, post, get, startup

    @pytest.mark.unit
    def test_without_a_link_the_alias_registers_alone(self, app):
        from satorineuron import VERSION
        result, post, get, startup = self._register(
            app, _manager(), {'success': True, 'peer_id': 7, 'wallet_address': None})
        (url,), kwargs = post.call_args
        assert url.endswith('/api/v1/peer/register')
        assert kwargs['headers'] == {
            'alias-pubkey': ALIAS_PUBKEY,
            'nostr-pubkey': NOSTR_PUBKEY,
            'neuron-version': VERSION}
        get.assert_not_called()  # no peer/login check
        assert result['peer_id'] == 7
        assert startup.linkedWalletAddress is None

    @pytest.mark.unit
    def test_with_a_link_it_sends_the_link_and_reads_the_wallet_address(self, app):
        import base64
        link = {'message': 'Satori: link this alias to my wallet\nalias: 0xA\nwallet: 0xW\nissued: 1',
                'signature': '0x' + 'cd' * 65}
        result, post, get, startup = self._register(
            app, _manager(link), {'success': True, 'peer_id': 7, 'wallet_address': '0xW'})
        headers = post.call_args.kwargs['headers']
        assert set(headers) == {
            'alias-pubkey', 'nostr-pubkey', 'neuron-version', 'wallet-link', 'wallet-signature'}
        assert base64.b64decode(headers['wallet-link']).decode('utf-8') == link['message']
        assert headers['wallet-signature'] == link['signature']
        get.assert_not_called()
        assert startup.linkedWalletAddress == '0xW'


# Download and import of the key files, by layout ##########################


@pytest.fixture
def keys_client(app, tmp_path):
    """(client, keys folder): a logged-in session whose keys folder is tmp_path."""
    import web.routes as routes
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['wallet_open'] = True
        sess['session_id'] = 'sid'
    manager = MagicMock()
    manager.wallet.isDecrypted = True
    with patch.dict(routes._session_wallets, {'sid': manager}), \
            patch('satorineuron.config.keysPath',
                  side_effect=lambda filename=None: str(tmp_path / filename) if filename else str(tmp_path)), \
            patch('satorineuron.config.get', return_value={}), \
            patch.object(routes, 'get_startup', return_value=MagicMock()):
        yield client, tmp_path


def _zip_names(response):
    import io
    import zipfile
    return sorted(zipfile.ZipFile(io.BytesIO(response.data)).namelist())


class TestKeyFilesDownload:

    @pytest.mark.unit
    def test_older_layout_downloads_wallet_and_vault(self, keys_client):
        client, keys = keys_client
        (keys / 'wallet.yaml').write_text('alias: 1\n')
        (keys / 'vault.yaml').write_text('wallet: 1\n')
        response = client.get('/api/wallet/download')
        assert response.status_code == 200
        assert _zip_names(response) == ['wallet/vault.yaml', 'wallet/wallet.yaml']

    @pytest.mark.unit
    def test_download_has_everything_needed_and_no_caches(self, keys_client):
        client, keys = keys_client
        for name in ('wallet.yaml', 'vault.yaml', 'vault.yaml.bak', 'wallet.scripts.json',
                     'vault.scripts.json', 'nostr.yaml', 'wallet-link.yaml'):
            (keys / name).write_text('x: 1\n')
        (keys / 'vault.cache.joblib').write_text('coins')
        (keys / 'backup_20260101_000000').mkdir()
        response = client.get('/api/wallet/download')
        assert _zip_names(response) == sorted('wallet/' + n for n in (
            'wallet.yaml', 'vault.yaml', 'vault.yaml.bak', 'wallet.scripts.json',
            'vault.scripts.json', 'nostr.yaml', 'wallet-link.yaml'))

    @pytest.mark.unit
    def test_newer_layout_downloads_alias_and_wallet(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('alias: 1\n')
        (keys / 'wallet.yaml').write_text('wallet: 1\n')
        (keys / 'alias.yaml.bak').write_text('alias: 1\n')
        response = client.get('/api/wallet/download')
        assert response.status_code == 200
        assert _zip_names(response) == [
            'wallet/alias.yaml', 'wallet/alias.yaml.bak', 'wallet/wallet.yaml']


def _key(tag):
    """A key file's body; tag makes it a different key."""
    return f'publicKey: pub-{tag}\nentropy: ent-{tag}\nprivateKey: priv-{tag}\n'


NOSTR = 'pubkey_hex: ab\nsecret_hex: cd\n'


class TestKeyFilesImport:

    def _upload(self, client, files):
        import io
        data = {'files': [(io.BytesIO(body.encode() if isinstance(body, str) else body), name)
                          for name, body in files.items()]}
        return client.post('/api/wallet/import', data=data, content_type='multipart/form-data')

    def _zip(self, files):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as z:
            for name, body in files.items():
                z.writestr(name, body)
        return buf.getvalue()

    @pytest.mark.unit
    def test_older_layout_upload_into_a_newer_install_is_written_by_role(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('old: alias\n')
        response = self._upload(client, {'wallet.yaml': _key('a'), 'vault.yaml': _key('w')})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'alias.yaml').read_text() == _key('a')
        assert (keys / 'wallet.yaml').read_text() == _key('w')
        assert not (keys / 'vault.yaml').exists()

    @pytest.mark.unit
    def test_newer_layout_upload_into_an_older_install_is_written_by_role(self, keys_client):
        client, keys = keys_client
        (keys / 'wallet.yaml').write_text('old: alias\n')
        (keys / 'vault.yaml').write_text('old: wallet\n')
        response = self._upload(client, {'alias.yaml': _key('a'), 'wallet.yaml': _key('w')})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'wallet.yaml').read_text() == _key('a')
        assert (keys / 'vault.yaml').read_text() == _key('w')
        assert not (keys / 'alias.yaml').exists()

    @pytest.mark.unit
    def test_upload_without_the_wallet_file_is_refused(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('old: alias\n')
        response = self._upload(client, {'alias.yaml': _key('a')})
        assert response.status_code == 400
        assert 'wallet.yaml' in response.get_json()['error']
        assert (keys / 'alias.yaml').read_text() == 'old: alias\n'

    @pytest.mark.unit
    def test_the_downloaded_zip_imports_as_is(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text(_key('old-a'))
        (keys / 'wallet.yaml').write_text(_key('old-w'))
        backup = self._zip({'wallet/wallet.yaml': _key('a'), 'wallet/vault.yaml': _key('w'),
                            'wallet/vault.yaml.bak': _key('w'), 'wallet/nostr.yaml': NOSTR})
        response = self._upload(client, {'wallet_backup.zip': backup})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'alias.yaml').read_text() == _key('a')
        assert (keys / 'wallet.yaml').read_text() == _key('w')
        assert (keys / 'wallet.yaml.bak').read_text() == _key('w')
        assert (keys / 'nostr.yaml').read_text() == NOSTR

    @pytest.mark.unit
    def test_download_then_import_round_trip(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text(_key('a'))
        (keys / 'wallet.yaml').write_text(_key('w'))
        (keys / 'nostr.yaml').write_text(NOSTR)
        backup = client.get('/api/wallet/download').data
        for f in ('alias.yaml', 'wallet.yaml', 'nostr.yaml'):
            (keys / f).write_text('replaced: true\n')
        response = self._upload(client, {'wallet_backup.zip': backup})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'alias.yaml').read_text() == _key('a')
        assert (keys / 'wallet.yaml').read_text() == _key('w')
        assert (keys / 'nostr.yaml').read_text() == NOSTR

    @pytest.mark.unit
    def test_without_a_nostr_key_this_neurons_is_kept(self, keys_client):
        client, keys = keys_client
        (keys / 'nostr.yaml').write_text('pubkey_hex: mine\nsecret_hex: mine\n')
        response = self._upload(client, {'alias.yaml': _key('a'), 'wallet.yaml': _key('w')})
        assert response.status_code == 200, response.get_json()
        assert 'mine' in (keys / 'nostr.yaml').read_text()

    @pytest.mark.unit
    def test_coin_caches_go_but_channel_scripts_are_never_deleted(self, keys_client):
        client, keys = keys_client
        (keys / 'wallet.yaml').write_text(_key('old-a'))
        (keys / 'vault.yaml').write_text(_key('old-w'))
        for cache in ('wallet.cache.joblib', 'vault.cache.joblib'):
            (keys / cache).write_text('old coins')
        for scripts in ('wallet.scripts.json', 'vault.scripts.json'):
            (keys / scripts).write_text('{"p2sh": {"status": "funded"}}')
        response = self._upload(client, {'wallet.yaml': _key('old-a'), 'vault.yaml': _key('old-w')})
        assert response.status_code == 200, response.get_json()
        assert not list(keys.glob('*.cache.joblib'))
        assert (keys / 'wallet.scripts.json').read_text() == '{"p2sh": {"status": "funded"}}'
        assert (keys / 'vault.scripts.json').read_text() == '{"p2sh": {"status": "funded"}}'
        backup = Path(response.get_json()['backup_location'])
        assert (backup / 'vault.cache.joblib').read_text() == 'old coins'

    @pytest.mark.unit
    def test_channel_scripts_and_link_in_a_backup_are_restored_by_role(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text(_key('old-a'))
        (keys / 'wallet.yaml').write_text(_key('old-w'))
        backup = self._zip({'wallet/wallet.yaml': _key('a'), 'wallet/vault.yaml': _key('w'),
                            'wallet/wallet.scripts.json': '{"alias-channel": {}}',
                            'wallet/vault.scripts.json': '{"wallet-channel": {}}',
                            'wallet/wallet-link.yaml': 'message: m\nsignature: s\n'})
        response = self._upload(client, {'b.zip': backup})
        assert response.status_code == 200, response.get_json()
        # old-layout names in the backup, newer layout here: scripts follow their key
        assert (keys / 'alias.scripts.json').read_text() == '{"alias-channel": {}}'
        assert (keys / 'wallet.scripts.json').read_text() == '{"wallet-channel": {}}'
        assert (keys / 'wallet-link.yaml').read_text() == 'message: m\nsignature: s\n'

    @pytest.mark.unit
    def test_a_broken_scripts_file_is_refused(self, keys_client):
        client, keys = keys_client
        response = self._upload(client, {'alias.yaml': _key('a'), 'wallet.yaml': _key('w'),
                                         'alias.scripts.json': 'not json'})
        assert response.status_code == 400
        assert 'payment channel scripts' in response.get_json()['error']

    @pytest.mark.unit
    @pytest.mark.parametrize('files, why', [
        ({'alias.yaml': 'ui port: 24601\n', 'wallet.yaml': _key('w')}, 'not an alias or wallet key file'),
        ({'alias.yaml': _key('same'), 'wallet.yaml': _key('same')}, 'same key'),
        ({'alias.yaml': _key('a'), 'wallet.yaml': _key('w'), 'nostr.yaml': 'x: 1\n'}, 'not a Nostr key file'),
        ({'alias.yaml': _key('a'), 'config.yaml': _key('w')}, 'Missing required files'),
        ({'notes.txt': 'hi'}, 'not a wallet file'),
    ])
    def test_wrong_files_are_refused_and_nothing_changes(self, keys_client, files, why):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text(_key('old-a'))
        (keys / 'wallet.yaml').write_text(_key('old-w'))
        response = self._upload(client, files)
        assert response.status_code == 400
        assert why in response.get_json()['error']
        assert (keys / 'alias.yaml').read_text() == _key('old-a')
        assert (keys / 'wallet.yaml').read_text() == _key('old-w')

    @pytest.mark.unit
    def test_a_zip_with_other_files_is_refused(self, keys_client):
        client, keys = keys_client
        backup = self._zip({'wallet/alias.yaml': _key('a'), 'wallet/wallet.yaml': _key('w'), 'evil.sh': 'rm -rf /'})
        response = self._upload(client, {'backup.zip': backup})
        assert response.status_code == 400
        assert 'not a wallet file' in response.get_json()['error']

    @pytest.mark.unit
    def test_check_says_what_each_file_is_and_changes_nothing(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text(_key('old-a'))
        (keys / 'wallet.yaml').write_text(_key('old-w'))
        backup = self._zip({'wallet/wallet.yaml': _key('a'), 'wallet/vault.yaml': _key('w'),
                            'wallet/vault.yaml.bak': _key('w'), 'wallet/nostr.yaml': NOSTR})
        import io
        response = client.post('/api/wallet/import?check=1', content_type='multipart/form-data',
                               data={'files': [(io.BytesIO(backup), 'wallet_backup.zip')]})
        assert response.status_code == 200, response.get_json()
        assert response.get_json()['files'] == [
            {'name': 'wallet.yaml', 'role': 'Alias'},
            {'name': 'vault.yaml', 'role': 'Wallet'},
            {'name': 'vault.yaml.bak', 'role': 'Wallet (spare copy)'},
            {'name': 'nostr.yaml', 'role': 'Nostr identity'},
        ]
        assert (keys / 'alias.yaml').read_text() == _key('old-a')
        assert not list(keys.glob('backup_*'))

    @pytest.mark.unit
    def test_import_clears_the_old_aliases_last_prediction_time(self, keys_client):
        client, keys = keys_client
        import web.routes as routes  # noqa: F401
        cfg = {'base predict last submit ts': 123, 'reward address': '0xabc', 'ui port': 1}
        with patch('satorineuron.config.get', side_effect=lambda *a, **k: dict(cfg)), \
                patch('satorineuron.config.put', side_effect=lambda *a, data=None, **k: (cfg.clear(), cfg.update(data))):
            response = self._upload(client, {'alias.yaml': _key('a'), 'wallet.yaml': _key('w')})
        assert response.status_code == 200, response.get_json()
        assert cfg == {'ui port': 1}
