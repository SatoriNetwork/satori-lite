"""
Unit tests for session-specific vault management.

Tests that each browser session gets its own WalletManager instance
and vault unlock state is isolated per session.
"""
import pytest
from unittest.mock import MagicMock, patch, Mock
from cryptography.fernet import Fernet


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


class TestSessionVaultManagement:
    """Test session-specific vault management."""

    @pytest.mark.unit
    def test_session_id_generated_on_first_access(self, client):
        """Session ID should be generated on first access."""
        with client.session_transaction() as sess:
            # Initially no session_id
            assert 'session_id' not in sess

        # Access a protected route (will redirect to login, but should set session_id)
        client.get('/dashboard')

        with client.session_transaction() as sess:
            # Session ID should now exist
            assert 'session_id' in sess
            assert len(sess['session_id']) > 0

    @pytest.mark.unit
    def test_different_sessions_get_different_ids(self, app):
        """Different clients should get different session IDs."""
        client1 = app.test_client()
        client2 = app.test_client()

        client1.get('/dashboard')
        client2.get('/dashboard')

        with client1.session_transaction() as sess1:
            session_id_1 = sess1.get('session_id')

        with client2.session_transaction() as sess2:
            session_id_2 = sess2.get('session_id')

        assert session_id_1 != session_id_2

    @pytest.mark.unit
    def test_session_vault_created_on_login(self, client):
        """Each session should get its own WalletManager instance."""
        mock_wallet_manager = MagicMock()
        mock_vault = MagicMock()
        mock_vault.isDecrypted = True
        mock_wallet_manager.openVault.return_value = mock_vault

        # Mock vault password check and session vault creation
        with patch('web.routes.check_vault_password_exists', return_value=True):
            with patch('web.routes.get_or_create_session_vault', return_value=mock_wallet_manager):
                response = client.post('/login', data={'password': 'test_password'})

        # Should have created a WalletManager for this session
        assert response.status_code in [302, 303]

        with client.session_transaction() as sess:
            assert sess.get('vault_open') is True

    @pytest.mark.unit
    def test_multiple_sessions_get_separate_vaults(self, app):
        """Multiple sessions should each get their own vault instance."""
        client1 = app.test_client()
        client2 = app.test_client()

        mock_vault1 = MagicMock()
        mock_vault1.isDecrypted = True
        mock_vault2 = MagicMock()
        mock_vault2.isDecrypted = True

        # Create different wallet managers for each call
        call_count = 0
        def create_wallet_manager():
            nonlocal call_count
            call_count += 1
            manager = MagicMock()
            if call_count == 1:
                manager.openVault.return_value = mock_vault1
            else:
                manager.openVault.return_value = mock_vault2
            return manager

        # Login with both clients
        with patch('web.routes.check_vault_password_exists', return_value=True):
            with patch('web.routes.get_or_create_session_vault', side_effect=create_wallet_manager):
                client1.post('/login', data={'password': 'test_password'})
                client2.post('/login', data={'password': 'test_password'})

        # Both sessions should be logged in (this verifies separate vaults were created)
        assert call_count == 2

    @pytest.mark.unit
    def test_vault_password_encrypted_in_session(self, client):
        """Vault password should be encrypted when stored in session."""
        mock_wallet_manager = MagicMock()
        mock_vault = MagicMock()
        mock_vault.isDecrypted = True
        mock_wallet_manager.openVault.return_value = mock_vault

        with patch('web.routes.check_vault_password_exists', return_value=True):
            with patch('web.routes.get_or_create_session_vault', return_value=mock_wallet_manager):
                response = client.post('/login', data={'password': 'my_secret_password'})

        assert response.status_code in [302, 303]

        with client.session_transaction() as sess:
            # Password should be encrypted in session
            assert 'encrypted_vault_password' in sess
            assert 'session_key' in sess
            # Should NOT store plaintext password
            assert 'vault_password' not in sess
            # Encrypted password should not be the plaintext
            assert sess['encrypted_vault_password'] != 'my_secret_password'

    @pytest.mark.unit
    def test_decrypt_vault_password_from_session(self, client):
        """Should be able to decrypt vault password from session."""
        # Simulate encrypted password in session
        password = 'my_secret_password'
        session_key = Fernet.generate_key()
        f = Fernet(session_key)
        encrypted_pw = f.encrypt(password.encode())

        with client.session_transaction() as sess:
            sess['encrypted_vault_password'] = encrypted_pw.decode()
            sess['session_key'] = session_key.decode()

        # Function to decrypt should work
        from web.routes import decrypt_vault_password_from_session

        with client:
            client.get('/dashboard')  # Establish session context
            decrypted = decrypt_vault_password_from_session()
            assert decrypted == password

    @pytest.mark.unit
    @patch('web.routes.cleanup_session_vault')
    def test_logout_cleans_up_session_vault(self, mock_cleanup, client):
        """Logout should cleanup the session's vault."""
        # Setup session
        with client.session_transaction() as sess:
            sess['session_id'] = 'test-session-123'
            sess['vault_open'] = True

        # Logout
        response = client.get('/logout')

        # Should have called cleanup
        mock_cleanup.assert_called_once_with('test-session-123')

        # Session should be cleared
        with client.session_transaction() as sess:
            assert 'vault_open' not in sess

    @pytest.mark.unit
    def test_session_vault_isolation_on_logout(self, app):
        """One session logging out should not affect other sessions."""
        client1 = app.test_client()
        client2 = app.test_client()

        mock_wallet_manager = MagicMock()
        mock_vault = MagicMock()
        mock_vault.isDecrypted = True
        mock_wallet_manager.openVault.return_value = mock_vault

        # Login both sessions
        with patch('web.routes.check_vault_password_exists', return_value=True):
            with patch('web.routes.get_or_create_session_vault', return_value=mock_wallet_manager):
                client1.post('/login', data={'password': 'test_password'})
                client2.post('/login', data={'password': 'test_password'})

        # Verify both are logged in
        with client1.session_transaction() as sess1:
            assert sess1.get('vault_open') is True

        with client2.session_transaction() as sess2:
            assert sess2.get('vault_open') is True

        # Logout client1
        client1.get('/logout')

        # Client1 should be logged out
        with client1.session_transaction() as sess1:
            assert sess1.get('vault_open') is not True

        # Client2 should STILL be logged in
        with client2.session_transaction() as sess2:
            assert sess2.get('vault_open') is True


class TestVaultOnDemandOperations:
    """Test vault unlock on-demand for specific operations."""

    @pytest.mark.unit
    @patch('web.routes.get_or_create_session_vault')
    def test_private_key_endpoint_uses_session_vault(self, mock_get_vault, client):
        """Private key endpoint should use session-specific vault."""
        mock_wallet_manager = MagicMock()
        mock_vault = MagicMock()
        mock_vault.privkey = 'test_private_key'
        mock_wallet_manager.vault = mock_vault
        mock_get_vault.return_value = mock_wallet_manager

        # Setup logged in session
        with client.session_transaction() as sess:
            sess['vault_open'] = True
            sess['session_id'] = 'test-session'

        response = client.get('/api/wallet/private-key')

        # Should have gotten session vault
        mock_get_vault.assert_called_once()

    @pytest.mark.unit
    @patch('web.routes.get_or_create_session_vault')
    def test_send_transaction_uses_session_vault(self, mock_get_vault, client):
        """Send transaction should use session-specific vault for signing."""
        mock_wallet_manager = MagicMock()
        mock_wallet_manager.vault.send.return_value = {'txHash': 'test_hash'}
        mock_get_vault.return_value = mock_wallet_manager

        # Setup logged in session
        with client.session_transaction() as sess:
            sess['vault_open'] = True
            sess['session_id'] = 'test-session'

        response = client.post('/api/wallet/send', json={
            'address': 'test_address',
            'amount': 10
        })

        # Should have used session vault
        mock_get_vault.assert_called_once()
