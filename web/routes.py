"""
Satori-Lite Web UI Routes.

Handles all web routes for the minimal UI:
- Login/vault unlock
- Dashboard
- API proxy endpoints
"""
from functools import wraps
import time
import logging
import base64
import requests
import uuid
from threading import Lock
from cryptography.fernet import Fernet
from flask import (
    render_template,
    redirect,
    url_for,
    request,
    session,
    flash,
    jsonify,
    current_app
)

logger = logging.getLogger(__name__)

# Global vault reference (will be set by the application) - used by background processes
_startup_vault = None

# Session-specific vaults - each browser session gets its own WalletManager
_session_vaults = {}
_vault_lock = Lock()


def check_vault_file_exists():
    """Check if vault.yaml file exists.

    Returns:
        bool: True if vault.yaml file exists, False otherwise
    """
    try:
        from satorineuron import config
        import os
        vault_path = config.walletPath('vault.yaml')
        return os.path.exists(vault_path)
    except Exception as e:
        logger.warning(f"Failed to check vault file: {e}")
        return False


def set_vault(vault):
    """Set the startup vault instance (used by background processes)."""
    global _startup_vault
    _startup_vault = vault


def get_vault():
    """Get the startup vault instance (for backward compatibility)."""
    return _startup_vault


def get_or_create_session_vault():
    """Get or create WalletManager for current session.

    Each browser session gets its own WalletManager instance.
    This provides true session isolation - one user's logout doesn't
    affect another user's session.

    Returns:
        WalletManager instance for this session
    """
    # Get or generate session ID
    session_id = session.get('session_id')
    if not session_id:
        session_id = str(uuid.uuid4())
        session['session_id'] = session_id
        session.permanent = False  # Don't persist sessions indefinitely
        logger.info(f"Generated new session ID: {session_id}")

    # Get or create vault for this session
    with _vault_lock:
        if session_id not in _session_vaults:
            try:
                from satorineuron.init.wallet import WalletManager
                from satorineuron import config

                # Create NEW WalletManager instance for this session
                # Don't auto-decrypt vault - user must provide password via login
                logger.info(f"Creating new WalletManager for session: {session_id}")
                _session_vaults[session_id] = WalletManager.create(
                    walletPath=config.walletPath('wallet.yaml'),
                    vaultPath=config.walletPath('vault.yaml'),
                    useConfigPassword=False  # Don't auto-decrypt with config password
                    # cachePath is optional and defaults to None
                )
            except Exception as e:
                logger.error(f"Failed to create session vault: {e}")
                return None

    return _session_vaults[session_id]


def cleanup_session_vault(session_id):
    """Clean up vault when session ends.

    Args:
        session_id: The session ID to cleanup
    """
    with _vault_lock:
        if session_id in _session_vaults:
            try:
                logger.info(f"Cleaning up session vault: {session_id}")
                _session_vaults[session_id].closeVault()
            except Exception as e:
                logger.warning(f"Error closing vault for session {session_id}: {e}")

            try:
                del _session_vaults[session_id]
            except Exception as e:
                logger.warning(f"Error deleting session vault {session_id}: {e}")


def encrypt_vault_password(password):
    """Encrypt vault password for storage in session.

    Args:
        password: Plaintext password

    Returns:
        tuple: (encrypted_password, session_key) both as strings
    """
    session_key = Fernet.generate_key()
    f = Fernet(session_key)
    encrypted_pw = f.encrypt(password.encode())
    return encrypted_pw.decode(), session_key.decode()


def decrypt_vault_password_from_session():
    """Decrypt vault password from current session.

    Returns:
        str: Decrypted password, or None if not available
    """
    try:
        encrypted_pw = session.get('encrypted_vault_password')
        session_key = session.get('session_key')

        if not encrypted_pw or not session_key:
            return None

        f = Fernet(session_key.encode())
        password = f.decrypt(encrypted_pw.encode()).decode()
        return password
    except Exception as e:
        logger.error(f"Failed to decrypt vault password: {e}")
        return None


def ensure_peer_registered(app, wallet_manager, max_retries=3):
    """Ensure the peer is registered with the API server.

    This function should be called after vault is successfully unlocked.
    It will:
    1. Try to login (check if peer exists)
    2. If not found, register the peer
    3. Retry with delay if registration fails

    Args:
        app: Flask app instance for config access
        wallet_manager: The wallet manager instance with wallet and vault
        max_retries: Maximum number of retry attempts

    Returns:
        dict with peer info if successful, None if failed
    """
    api_url = app.config.get('SATORI_API_URL', 'http://satori-api:8000')

    # Get wallet pubkey (identity)
    wallet_pubkey = None
    if wallet_manager.wallet and hasattr(wallet_manager.wallet, 'pubkey'):
        wallet_pubkey = wallet_manager.wallet.pubkey

    # Get vault pubkey
    vault_pubkey = None
    if wallet_manager.vault and hasattr(wallet_manager.vault, 'pubkey'):
        vault_pubkey = wallet_manager.vault.pubkey

    if not wallet_pubkey:
        logger.warning("No wallet pubkey available for peer registration")
        return None

    for attempt in range(max_retries):
        try:
            # Step 1: Try login first
            headers = {'wallet-pubkey': wallet_pubkey}
            resp = requests.get(
                f"{api_url}/api/v1/peer/login",
                headers=headers,
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get('exists'):
                    logger.info(f"Peer already registered: peer_id={data.get('peer_id')}")
                    return data

            # Step 2: Peer not found, register
            headers = {'wallet-pubkey': wallet_pubkey}
            if vault_pubkey:
                headers['vault-pubkey'] = vault_pubkey

            resp = requests.post(
                f"{api_url}/api/v1/peer/register",
                headers=headers,
                timeout=10
            )

            if resp.status_code == 200:
                data = resp.json()
                if data.get('success'):
                    logger.info(f"Peer registered: peer_id={data.get('peer_id')}")
                    return data

            logger.warning(f"Peer registration attempt {attempt + 1} failed: {resp.text}")

        except requests.RequestException as e:
            logger.warning(f"Peer registration attempt {attempt + 1} error: {e}")

        # Wait before retry (except on last attempt)
        if attempt < max_retries - 1:
            time.sleep(5)

    logger.error("Failed to register peer after all retries")
    return None


def login_required(f):
    """Decorator to require login for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Ensure session ID exists for tracking
        if not session.get('session_id'):
            session['session_id'] = str(uuid.uuid4())
            session.permanent = False  # Don't persist sessions indefinitely

        # Check if user is logged in via session flag
        if not session.get('vault_open'):
            # Not logged in - check if vault file exists
            if not check_vault_file_exists():
                # No vault file - redirect to create password
                return redirect(url_for('vault_setup'))
            # Vault exists but not logged in - redirect to login
            return redirect(url_for('login'))

        # Validate that vault actually exists and is open (handles container restart)
        # Check _session_vaults directly - don't call get_or_create (which creates new vault)
        session_id = session.get('session_id')
        if session_id and session_id not in _session_vaults:
            # Vault doesn't exist (container restarted) - require re-login
            logger.info(f"Session vault missing for {session_id} - forcing re-login")
            session.pop('vault_open', None)
            session['logged_out'] = True  # Prevent auto-login
            return redirect(url_for('login'))

        # Also validate vault is actually decrypted
        if session_id and session_id in _session_vaults:
            wallet_manager = _session_vaults[session_id]
            if not wallet_manager or not wallet_manager.vault or not wallet_manager.vault.isDecrypted:
                logger.info(f"Session vault not decrypted for {session_id} - forcing re-login")
                session.pop('vault_open', None)
                session['logged_out'] = True  # Prevent auto-login
                return redirect(url_for('login'))

        return f(*args, **kwargs)
    return decorated_function


def register_routes(app):
    """Register all routes with the Flask app."""

    @app.route('/')
    def index():
        """Redirect to dashboard if logged in, vault setup if needed, otherwise to login."""
        # Check if vault file exists first
        if not check_vault_file_exists():
            return redirect(url_for('vault_setup'))

        if session.get('vault_open'):
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Handle vault unlock/login."""
        # Check if vault file exists, redirect to setup if not
        if not check_vault_file_exists():
            return redirect(url_for('vault_setup'))

        # Auto-login on GET if config password exists
        if request.method == 'GET':
            # Skip auto-login if user just logged out explicitly
            if session.get('logged_out'):
                return render_template('login.html')

            from satorineuron import config
            config_password = config.get().get('vault password')

            if config_password:
                # Attempt auto-login with config password
                wallet_manager = get_or_create_session_vault()
                if wallet_manager:
                    try:
                        vault = wallet_manager.openVault(password=config_password, create=True)

                        if vault and vault.isDecrypted:
                            # Successfully auto-logged in
                            session['vault_open'] = True
                            session.pop('logged_out', None)  # Clear logout flag

                            # Encrypt and store password in session for future use
                            encrypted_pw, session_key = encrypt_vault_password(config_password)
                            session['encrypted_vault_password'] = encrypted_pw
                            session['session_key'] = session_key

                            # Register peer with API server (non-blocking)
                            try:
                                peer_info = ensure_peer_registered(
                                    current_app, wallet_manager, max_retries=1)
                                if peer_info:
                                    session['peer_id'] = peer_info.get('peer_id')
                                    logger.info(f"Auto-login successful, peer registered: {peer_info}")
                            except Exception as e:
                                logger.warning(f"Auto-login peer registration warning: {e}")

                            # Auto-login successful - redirect to dashboard
                            logger.info("Auto-login successful with config password")
                            return redirect(url_for('dashboard'))
                    except Exception as e:
                        logger.warning(f"Auto-login failed: {e}")
                        # Fall through to show login form

            # No config password or auto-login failed - show login form
            return render_template('login.html')

        if request.method == 'POST':
            password = request.form.get('password', '')

            # Get session-specific wallet manager
            wallet_manager = get_or_create_session_vault()
            if wallet_manager:
                try:
                    # WalletManager.openVault(password) unlocks the vault
                    vault = wallet_manager.openVault(password=password, create=True)

                    # Verify the vault was actually decrypted
                    # If wrong password, decryption fails silently and vault remains encrypted
                    if vault is None:
                        flash('Error: Could not open vault', 'error')
                    elif not vault.isDecrypted:
                        # Wrong password - vault is still encrypted
                        flash('Error: Invalid password', 'error')
                    else:
                        # Successfully decrypted - vault stays unlocked for this session
                        session['vault_open'] = True
                        session.pop('logged_out', None)  # Clear logout flag

                        # Encrypt and store password in session for future use
                        encrypted_pw, session_key = encrypt_vault_password(password)
                        session['encrypted_vault_password'] = encrypted_pw
                        session['session_key'] = session_key

                        # Register peer with API server (non-blocking)
                        try:
                            peer_info = ensure_peer_registered(
                                current_app, wallet_manager, max_retries=1)
                            if peer_info:
                                session['peer_id'] = peer_info.get('peer_id')
                                logger.info(f"Peer registered: {peer_info}")
                        except Exception as e:
                            # Don't block login if registration fails
                            logger.warning(f"Peer registration warning: {e}")

                        return redirect(url_for('dashboard'))
                except Exception as e:
                    flash(f'Error: Invalid password or vault error', 'error')
            else:
                # For testing without vault
                if app.config.get('TESTING'):
                    session['vault_open'] = True
                    return redirect(url_for('dashboard'))
                flash('Error: Vault not initialized', 'error')

        return render_template('login.html')

    @app.route('/vault-setup', methods=['GET', 'POST'])
    def vault_setup():
        """Handle initial vault password creation."""
        # If vault file already exists, redirect to login with message
        if check_vault_file_exists():
            flash('Vault already exists. Please log in with your existing password.', 'info')
            logger.info("Attempted to access vault setup but password already exists")
            return redirect(url_for('login'))

        if request.method == 'POST':
            password = request.form.get('password', '')
            password_confirm = request.form.get('password_confirm', '')

            # Validate password length
            if len(password) < 4:
                flash('Password must be at least 4 characters long', 'error')
                return render_template('vault_setup.html')

            # Validate passwords match
            if password != password_confirm:
                flash('Passwords do not match. Please try again.', 'error')
                return render_template('vault_setup.html')

            # Create vault with password (DO NOT save password to config for security)
            try:
                # Get session-specific wallet manager to create the vault
                wallet_manager = get_or_create_session_vault()

                if wallet_manager:
                    # Create the vault with the password
                    vault = wallet_manager.openVault(password=password, create=True)

                    if vault and vault.isDecrypted:
                        flash('Vault created successfully! Please log in with your password.', 'success')
                        logger.info("Vault created via web UI (password NOT saved to config)")
                        return redirect(url_for('login'))
                    else:
                        flash('Error creating vault. Please try again.', 'error')
                        return render_template('vault_setup.html')
                else:
                    flash('Error initializing wallet manager', 'error')
                    return render_template('vault_setup.html')
            except Exception as e:
                logger.error(f"Failed to create vault: {e}")
                flash(f'Error creating vault: {str(e)}', 'error')
                return render_template('vault_setup.html')

        return render_template('vault_setup.html')

    @app.route('/logout')
    def logout():
        """Handle logout/vault lock."""
        # Cleanup session-specific vault
        session_id = session.get('session_id')
        if session_id:
            cleanup_session_vault(session_id)

        # Set flag before clearing to prevent auto-login
        session.clear()
        session['logged_out'] = True
        return redirect(url_for('login'))

    @app.route('/dashboard')
    @login_required
    def dashboard():
        """Main dashboard page."""
        return render_template('dashboard.html')

    @app.route('/health')
    def health():
        """Health check endpoint - checks API server connectivity."""
        api_url = current_app.config.get('SATORI_API_URL', 'http://satori-api:8000')
        try:
            resp = requests.get(f"{api_url}/health", timeout=5)
            if resp.status_code == 200:
                return jsonify({'status': 'ok', 'api': 'connected'})
        except requests.RequestException:
            pass
        return jsonify({'status': 'ok', 'api': 'disconnected'})

    # API Proxy Routes
    def get_auth_headers():
        """Get authentication headers for API requests.

        This implements the challenge-response flow:
        1. Get a challenge token from the API
        2. Sign it with the identity wallet
        3. Return headers with pubkey, message, and signature
        """
        wallet_manager = get_or_create_session_vault()
        if not wallet_manager or not wallet_manager.wallet:
            return None

        api_url = current_app.config.get('SATORI_API_URL', 'http://satori-api:8000')

        try:
            # Step 1: Get challenge
            resp = requests.get(f"{api_url}/api/v1/auth/challenge", timeout=10)
            if resp.status_code != 200:
                logger.warning(f"Failed to get challenge: {resp.text}")
                return None

            challenge = resp.json().get('challenge')
            if not challenge:
                return None

            # Step 2: Get wallet pubkey and sign the challenge
            wallet = wallet_manager.wallet
            if not hasattr(wallet, 'pubkey') or not hasattr(wallet, 'sign'):
                logger.warning("Wallet missing pubkey or sign method")
                return None

            signature = wallet.sign(message=challenge)

            # Signature from wallet.sign() is already base64-encoded as bytes
            # Just decode to string - do NOT base64 encode again
            if isinstance(signature, bytes):
                signature = signature.decode('utf-8')

            # Step 3: Return auth headers
            return {
                'wallet-pubkey': wallet.pubkey,
                'message': challenge,
                'signature': signature
            }
        except Exception as e:
            logger.warning(f"Auth header generation failed: {e}")
            return None

    def proxy_api(endpoint, method='GET', data=None, authenticated=True):
        """Proxy requests to the Satori API server."""
        api_url = current_app.config.get('SATORI_API_URL', 'http://satori-api:8000')
        url = f"{api_url}/api/v1{endpoint}"

        # Get auth headers for authenticated requests
        headers = {}
        if authenticated:
            auth_headers = get_auth_headers()
            if auth_headers:
                headers.update(auth_headers)
            else:
                logger.warning(f"No auth headers available for {endpoint}")

        try:
            if method == 'GET':
                resp = requests.get(url, headers=headers, timeout=10)
            elif method == 'POST':
                resp = requests.post(url, json=data, headers=headers, timeout=10)
            elif method == 'DELETE':
                resp = requests.delete(url, json=data, headers=headers, timeout=10)
            else:
                return jsonify({'error': 'Invalid method'}), 400

            return jsonify(resp.json()), resp.status_code
        except requests.RequestException as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/balance/get')
    @login_required
    def api_balance():
        """Proxy balance request."""
        return proxy_api('/balance/get')

    @app.route('/api/peer/reward-address', methods=['GET', 'POST'])
    @login_required
    def api_reward_address():
        """Proxy reward address request."""
        if request.method == 'POST':
            return proxy_api('/peer/reward-address', 'POST', request.json)
        return proxy_api('/peer/reward-address')

    @app.route('/api/lender/status')
    @login_required
    def api_lender_status():
        """Proxy lender status request."""
        return proxy_api('/lender/status')

    @app.route('/api/lender/lend', methods=['POST', 'DELETE'])
    @login_required
    def api_lender_lend():
        """Proxy lend request."""
        data = request.get_json(silent=True) if request.method == 'POST' else None
        return proxy_api('/lender/lend', request.method, data)

    @app.route('/api/pool/worker', methods=['POST'])
    @login_required
    def api_pool_worker_add():
        """Proxy pool worker add request."""
        return proxy_api('/pool/worker', 'POST', request.json)

    @app.route('/api/pool/worker/<worker_address>', methods=['DELETE'])
    @login_required
    def api_pool_worker_delete(worker_address):
        """Proxy pool worker delete request."""
        return proxy_api(f'/pool/worker/{worker_address}', 'DELETE')

    @app.route('/api/pool/toggle-open', methods=['POST'])
    @login_required
    def api_pool_toggle():
        """Proxy pool toggle request."""
        return proxy_api('/pool/toggle-open', 'POST', request.json)

    @app.route('/api/pool/open', methods=['GET'])
    @login_required
    def api_pool_open():
        """Get list of open pools."""
        return proxy_api('/pool/open', 'GET')

    @app.route('/api/pool/commission', methods=['GET'])
    @login_required
    def api_pool_commission():
        """Get pool commission status."""
        return proxy_api('/pool/commission', 'GET')

    @app.route('/api/pool/workers', methods=['GET'])
    @login_required
    def api_pool_workers():
        """Get list of workers for authenticated user's pool."""
        return proxy_api('/pool/workers', 'GET')

    @app.route('/api/wallet/address')
    @login_required
    def api_wallet_address():
        """Get wallet and vault addresses."""
        wallet_manager = get_or_create_session_vault()
        if wallet_manager:
            result = {}
            # Get wallet address
            if wallet_manager.wallet and hasattr(wallet_manager.wallet, 'address'):
                result['wallet_address'] = wallet_manager.wallet.address
            # Get vault address
            if wallet_manager.vault and hasattr(wallet_manager.vault, 'address'):
                result['vault_address'] = wallet_manager.vault.address
            return jsonify(result)
        return jsonify({'error': 'Wallet not initialized'}), 500

    @app.route('/api/wallet/private-key')
    @login_required
    def api_wallet_private_key():
        """Get vault's private key (sensitive - requires login)."""
        wallet_manager = get_or_create_session_vault()
        if wallet_manager and wallet_manager.vault:
            try:
                privkey = wallet_manager.vault.privkey
                return jsonify({'private_key': privkey})
            except Exception as e:
                return jsonify({'error': str(e)}), 500
        return jsonify({'error': 'Vault not initialized'}), 500

    @app.route('/api/wallet/identity-private-key')
    @login_required
    def api_wallet_identity_private_key():
        """Get identity wallet's private key (for backup - requires login)."""
        wallet_manager = get_or_create_session_vault()
        if wallet_manager and wallet_manager.wallet:
            try:
                privkey = wallet_manager.wallet.privkey
                return jsonify({'private_key': privkey})
            except Exception as e:
                return jsonify({'error': str(e)}), 500
        return jsonify({'error': 'Identity wallet not initialized'}), 500

    @app.route('/api/wallet/send', methods=['POST'])
    @login_required
    def api_wallet_send():
        """Send SATORI tokens from vault."""
        wallet_manager = get_or_create_session_vault()
        if not wallet_manager or not wallet_manager.vault:
            return jsonify({'error': 'Vault not initialized'}), 500

        data = request.json or {}
        address = data.get('address', '').strip()
        amount = data.get('amount')
        sweep = data.get('sweep', False)

        # Validate address
        if not address:
            return jsonify({'error': 'Address is required'}), 400
        if not address.startswith('E') or len(address) != 34:
            return jsonify({'error': 'Invalid address format'}), 400

        # Validate amount (unless sweep)
        if not sweep:
            try:
                amount = float(amount)
                if amount <= 0:
                    return jsonify({'error': 'Amount must be greater than 0'}), 400
            except (TypeError, ValueError):
                return jsonify({'error': 'Invalid amount'}), 400

        try:
            vault = wallet_manager.vault
            # Get ready to send
            vault.get()
            vault.getReadyToSend()

            if sweep:
                # Send all tokens - returns string (txid)
                txid = vault.sendAllTransaction(address)
                if txid and len(txid) == 64:
                    return jsonify({'success': True, 'txid': txid})
                else:
                    return jsonify({'error': 'Transaction failed', 'details': str(txid)}), 500
            else:
                # Send specific amount - returns TransactionResult object
                result = vault.typicalNeuronTransaction(
                    amount=amount,
                    address=address)

                # Check if result is a TransactionResult object
                if hasattr(result, 'success') and hasattr(result, 'msg'):
                    if result.success and result.msg and len(result.msg) == 64:
                        return jsonify({'success': True, 'txid': result.msg})
                    else:
                        error_msg = result.msg or 'Transaction failed'
                        return jsonify({'error': error_msg}), 500
                # Fallback for unexpected return type
                else:
                    return jsonify({'error': 'Unexpected transaction result', 'details': str(result)}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/wallet/qr/<address>')
    @login_required
    def api_wallet_qr(address: str):
        """Generate QR code for an address."""
        import io
        import base64
        try:
            import qrcode
            qr = qrcode.QRCode(version=1, box_size=4, border=2)
            qr.add_data(address)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)
            img_base64 = base64.b64encode(buffer.getvalue()).decode()
            return jsonify({'qr_code': f'data:image/png;base64,{img_base64}'})
        except ImportError:
            return jsonify({'error': 'QR code library not available'}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/wallet/balance/direct')
    @login_required
    def api_wallet_balance_direct():
        """Get combined wallet and vault balance directly from electrumx.

        This bypasses the Satori API server and queries the blockchain directly
        via the electromax server using the wallet objects.
        """
        wallet_manager = get_or_create_session_vault()
        if not wallet_manager:
            return jsonify({'error': 'Wallet manager not initialized'}), 500

        try:
            # Ensure electrumx connection
            logger.info("Attempting to connect to ElectrumX...")
            if hasattr(wallet_manager, 'connect'):
                connected = wallet_manager.connect()
                logger.info(f"ElectrumX connection result: {connected}")
                if not connected:
                    logger.warning("Failed to connect to ElectrumX")
                    return jsonify({'error': 'Could not connect to electrumx'}), 500
            else:
                logger.warning("WalletManager has no connect method")

            total_satori = 0.0
            wallet_balance = 0.0
            vault_balance = 0.0
            total_evr = 0.0
            wallet_evr = 0.0
            vault_evr = 0.0

            # Get wallet (identity) balance
            if wallet_manager.wallet:
                wallet = wallet_manager.wallet
                if hasattr(wallet, 'getBalances'):
                    logger.info("Getting wallet balances...")
                    wallet.getBalances()
                    if hasattr(wallet, 'balance') and wallet.balance:
                        wallet_balance = wallet.balance.amount if hasattr(wallet.balance, 'amount') else 0.0
                    if hasattr(wallet, 'currency') and wallet.currency:
                        wallet_evr = wallet.currency.amount if hasattr(wallet.currency, 'amount') else 0.0
                    logger.info(f"Wallet balance: SATORI={wallet_balance}, EVR={wallet_evr}")

            # Get vault balance
            if wallet_manager.vault:
                vault = wallet_manager.vault
                if hasattr(vault, 'getBalances'):
                    logger.info("Getting vault balances...")
                    vault.getBalances()
                    if hasattr(vault, 'balance') and vault.balance:
                        vault_balance = vault.balance.amount if hasattr(vault.balance, 'amount') else 0.0
                    if hasattr(vault, 'currency') and vault.currency:
                        vault_evr = vault.currency.amount if hasattr(vault.currency, 'amount') else 0.0
                    logger.info(f"Vault balance: SATORI={vault_balance}, EVR={vault_evr}")

            total_satori = wallet_balance + vault_balance
            total_evr = wallet_evr + vault_evr

            return jsonify({
                'total': total_satori,
                'wallet_balance': wallet_balance,
                'vault_balance': vault_balance,
                'total_evr': total_evr,
                'wallet_evr': wallet_evr,
                'vault_evr': vault_evr
            })
        except Exception as e:
            import traceback
            logger.error(f"Failed to get direct balance: {e}")
            logger.error(f"Traceback: {traceback.format_exc()}")
            return jsonify({'error': str(e)}), 500
