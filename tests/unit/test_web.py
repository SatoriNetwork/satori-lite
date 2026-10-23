"""
Unit tests for neuron web UI.

Tests the Flask application, routes, and templates.
"""
import pytest
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
    def test_newer_layout_downloads_alias_and_wallet(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('alias: 1\n')
        (keys / 'wallet.yaml').write_text('wallet: 1\n')
        (keys / 'alias.yaml.bak').write_text('alias: 1\n')
        response = client.get('/api/wallet/download')
        assert response.status_code == 200
        assert _zip_names(response) == [
            'wallet/alias.yaml', 'wallet/alias.yaml.bak', 'wallet/wallet.yaml']


class TestKeyFilesImport:

    def _upload(self, client, files):
        import io
        data = {'files': [(io.BytesIO(body.encode()), name) for name, body in files.items()]}
        return client.post('/api/wallet/import', data=data, content_type='multipart/form-data')

    @pytest.mark.unit
    def test_older_layout_upload_into_a_newer_install_is_written_by_role(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('old: alias\n')
        response = self._upload(client, {'wallet.yaml': 'the: alias\n', 'vault.yaml': 'the: wallet\n'})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'alias.yaml').read_text() == 'the: alias\n'
        assert (keys / 'wallet.yaml').read_text() == 'the: wallet\n'
        assert not (keys / 'vault.yaml').exists()

    @pytest.mark.unit
    def test_newer_layout_upload_into_an_older_install_is_written_by_role(self, keys_client):
        client, keys = keys_client
        (keys / 'wallet.yaml').write_text('old: alias\n')
        (keys / 'vault.yaml').write_text('old: wallet\n')
        response = self._upload(client, {'alias.yaml': 'the: alias\n', 'wallet.yaml': 'the: wallet\n'})
        assert response.status_code == 200, response.get_json()
        assert (keys / 'wallet.yaml').read_text() == 'the: alias\n'
        assert (keys / 'vault.yaml').read_text() == 'the: wallet\n'
        assert not (keys / 'alias.yaml').exists()

    @pytest.mark.unit
    def test_upload_without_the_wallet_file_is_refused(self, keys_client):
        client, keys = keys_client
        (keys / 'alias.yaml').write_text('old: alias\n')
        response = self._upload(client, {'alias.yaml': 'the: alias\n'})
        assert response.status_code == 400
        assert 'wallet.yaml' in response.get_json()['error']
        assert (keys / 'alias.yaml').read_text() == 'old: alias\n'
