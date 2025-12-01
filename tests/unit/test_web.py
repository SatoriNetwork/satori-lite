"""
Unit tests for satori-lite web UI.

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


@pytest.fixture
def mock_vault():
    """Create mock vault for testing."""
    vault = MagicMock()
    vault.is_open = False
    vault.wallet = None
    return vault


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
    """Test login/vault unlock page."""

    @pytest.mark.unit
    def test_login_page_renders(self, client):
        """Login page should render successfully."""
        response = client.get('/login')
        assert response.status_code == 200
        assert b'password' in response.data.lower()

    @pytest.mark.unit
    def test_login_page_has_form(self, client):
        """Login page should contain a password form."""
        response = client.get('/login')
        assert b'<form' in response.data
        assert b'type="password"' in response.data

    @pytest.mark.unit
    def test_root_redirects_to_login_when_vault_locked(self, client):
        """Root URL should redirect to login when vault is locked."""
        response = client.get('/')
        assert response.status_code in [302, 303]
        assert b'/login' in response.data or '/login' in response.location


class TestVaultUnlock:
    """Test vault unlock functionality."""

    @pytest.mark.unit
    @patch('web.routes.get_vault')
    def test_login_with_correct_password_redirects_to_dashboard(self, mock_get_vault, client):
        """Correct password should unlock vault and redirect to dashboard."""
        mock_wallet_manager = MagicMock()
        mock_wallet_manager.openVault.return_value = MagicMock()
        mock_get_vault.return_value = mock_wallet_manager

        response = client.post('/login', data={'password': 'correct_password'})
        assert response.status_code in [302, 303]
        assert '/dashboard' in response.location or b'/dashboard' in response.data

    @pytest.mark.unit
    @patch('web.routes.get_vault')
    def test_login_with_incorrect_password_shows_error(self, mock_get_vault, client):
        """Incorrect password should show error message."""
        mock_wallet_manager = MagicMock()
        mock_wallet_manager.openVault.side_effect = Exception("Invalid password")
        mock_get_vault.return_value = mock_wallet_manager

        response = client.post('/login', data={'password': 'wrong_password'})
        assert response.status_code == 200
        assert b'error' in response.data.lower() or b'invalid' in response.data.lower()


class TestDashboard:
    """Test dashboard page."""

    @pytest.mark.unit
    def test_dashboard_requires_login(self, client):
        """Dashboard should redirect to login when not authenticated."""
        response = client.get('/dashboard')
        assert response.status_code in [302, 303]
        assert '/login' in response.location or b'/login' in response.data

    @pytest.mark.unit
    @patch('web.routes.get_vault')
    def test_dashboard_renders_when_logged_in(self, mock_get_vault, client):
        """Dashboard should render when vault is open."""
        mock_vault = MagicMock()
        mock_vault.is_open = True
        mock_vault.wallet = MagicMock()
        mock_get_vault.return_value = mock_vault

        # Simulate logged in session
        with client.session_transaction() as sess:
            sess['vault_open'] = True

        response = client.get('/dashboard')
        assert response.status_code == 200
        assert b'dashboard' in response.data.lower()


class TestLogout:
    """Test logout functionality."""

    @pytest.mark.unit
    @patch('web.routes.get_vault')
    def test_logout_clears_session(self, mock_get_vault, client):
        """Logout should clear session and redirect to login."""
        mock_vault = MagicMock()
        mock_get_vault.return_value = mock_vault

        # Simulate logged in session
        with client.session_transaction() as sess:
            sess['vault_open'] = True

        response = client.get('/logout')
        assert response.status_code in [302, 303]
        assert '/login' in response.location or b'/login' in response.data


class TestHealthCheck:
    """Test health check endpoint."""

    @pytest.mark.unit
    def test_health_endpoint_returns_ok(self, client):
        """Health endpoint should return status ok."""
        response = client.get('/health')
        assert response.status_code == 200
        data = response.get_json()
        assert data['status'] == 'ok'
