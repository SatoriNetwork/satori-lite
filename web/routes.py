"""
Satori-Lite Web UI Routes.

Handles all web routes for the minimal UI:
- Login/vault unlock
- Dashboard
- API proxy endpoints
"""
from functools import wraps
import os
import time
import logging
import base64
import os
import platform
import socket
import datetime
import requests
import uuid
from threading import Lock
from cryptography.fernet import Fernet
from satorilib.config import get_api_url

MUNDO_URL = os.environ.get('MUNDO_URL', 'https://mundo.satorinet.org')


def _mundoRequestSimplePartial(network: str, inputCount: int, outputCount: int) -> dict:
    """Call Mundo's request endpoint to get fee data for a partial transaction."""
    resp = requests.get(
        f'{MUNDO_URL}/simple_partial/request/{network}',
        params={'inputCount': inputCount, 'outputCount': outputCount},
        timeout=15)
    resp.raise_for_status()
    return resp.json()


def _mundoBroadcastSimplePartial(
    tx, reportedFeeSats: int, feeSatsReserved: int, network: str,
) -> str:
    """Call Mundo's broadcast endpoint with signOnly=true, returns signed tx hex."""
    txData = tx if isinstance(tx, str) else tx.hex()
    resp = requests.post(
        f'{MUNDO_URL}/simple_partial/broadcast/{network}/{feeSatsReserved}/{reportedFeeSats}/0',
        params={'signOnly': 'true'},
        data=txData,
        headers={'Content-Type': 'text/plain'},
        timeout=30)
    resp.raise_for_status()
    return resp.text
from flask import (
    render_template,
    redirect,
    url_for,
    request,
    session,
    flash,
    jsonify,
    current_app,
    send_file
)

logger = logging.getLogger(__name__)
_process_started_at = time.time()

# Global vault reference (will be set by the application) - used by background processes
_startup_vault = None

# Global startup instance reference (will be set by the application)
_startup_instance = None

# Session-specific vaults - each browser session gets its own WalletManager
_session_vaults = {}
_vault_lock = Lock()

# JWT authentication lock to prevent concurrent login attempts
_auth_lock = Lock()


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


def set_startup(startup):
    """Set the startup instance (used by web routes to access AI engine, etc)."""
    global _startup_instance
    _startup_instance = startup


def get_startup():
    """Get the startup instance."""
    return _startup_instance


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
        wallet_manager: The wallet manager instance with wallet and vault (both must be decrypted)
        max_retries: Maximum number of retry attempts

    Returns:
        dict with peer info if successful, None if failed
    """
    api_url = app.config.get('SATORI_API_URL', get_api_url())

    # Validate wallet exists
    if not wallet_manager.wallet:
        logger.error("Wallet not initialized - cannot register peer")
        return None

    # Validate vault exists and is decrypted
    if not wallet_manager.vault:
        logger.error("Vault not initialized - cannot register peer")
        return None

    if not hasattr(wallet_manager.vault, 'isDecrypted') or not wallet_manager.vault.isDecrypted:
        logger.error("Vault is not decrypted - cannot register peer")
        return None

    # Get wallet pubkey (identity) - REQUIRED
    wallet_pubkey = None
    if hasattr(wallet_manager.wallet, 'pubkey'):
        wallet_pubkey = wallet_manager.wallet.pubkey

    # Get vault pubkey - REQUIRED (not optional)
    vault_pubkey = None
    if hasattr(wallet_manager.vault, 'pubkey'):
        vault_pubkey = wallet_manager.vault.pubkey

    # Validate both pubkeys are present
    if not wallet_pubkey:
        logger.error("Wallet pubkey not available - cannot register peer")
        return None

    if not vault_pubkey:
        logger.error("Vault pubkey not available - cannot register peer")
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
            # POST /api/v1/peer/register
            # Required headers:
            #   - wallet-pubkey: The identity wallet's public key (REQUIRED)
            #   - vault-pubkey: The vault wallet's public key (REQUIRED - must be decrypted)
            headers = {
                'wallet-pubkey': wallet_pubkey,
                'vault-pubkey': vault_pubkey
            }

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
        from satorineuron import VERSION

        def render_login():
            return render_template('login.html', version=VERSION)

        # Check if vault file exists, redirect to setup if not
        if not check_vault_file_exists():
            return redirect(url_for('vault_setup'))

        # Auto-login on GET if config password exists
        if request.method == 'GET':
            # Skip auto-login if user just logged out explicitly
            if session.get('logged_out'):
                return render_login()

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
            return render_login()

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

        return render_login()

    @app.route('/vault-setup', methods=['GET', 'POST'])
    def vault_setup():
        """Handle initial vault password creation."""
        from satorineuron import VERSION

        def render_vault_setup():
            return render_template('vault_setup.html', version=VERSION)

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
                return render_vault_setup()

            # Validate passwords match
            if password != password_confirm:
                flash('Passwords do not match. Please try again.', 'error')
                return render_vault_setup()

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
                        return render_vault_setup()
                else:
                    flash('Error initializing wallet manager', 'error')
                    return render_vault_setup()
            except Exception as e:
                logger.error(f"Failed to create vault: {e}")
                flash(f'Error creating vault: {str(e)}', 'error')
                return render_vault_setup()

        return render_vault_setup()

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
        from satorineuron import VERSION

        # Derive eth_wallet_address locally from wallet pubkey
        eth_wallet_address = None
        try:
            wallet_manager = get_or_create_session_vault()
            if wallet_manager and wallet_manager.wallet and hasattr(wallet_manager.wallet, 'pubkey'):
                wallet_pubkey = wallet_manager.wallet.pubkey
                if wallet_pubkey:
                    from satorineuron.common.eth_address import derive_eth_wallet_address_from_pubkey
                    eth_wallet_address = derive_eth_wallet_address_from_pubkey(wallet_pubkey)
        except Exception as e:
            logger.warning(f"Could not derive eth_wallet_address: {e}")

        # Get nostr pubkey and relay URL from startup instance
        nostr_pubkey = None
        relay_url = None
        try:
            startup = get_startup()
            if startup and hasattr(startup, 'nostrPubkey'):
                nostr_pubkey = startup.nostrPubkey
            if startup and hasattr(startup, 'server') and startup.server:
                relay_url = getattr(startup.server, 'relayUrl', None)
        except Exception as e:
            logger.warning(f"Could not get nostr/relay info: {e}")

        return render_template(
            'dashboard.html',
            version=VERSION,
            eth_wallet_address=eth_wallet_address,
            nostr_pubkey=nostr_pubkey,
            relay_url=relay_url,
            page_mode='dashboard')

    @app.route('/p2p')
    @login_required
    def p2p_page():
        """P2P page."""
        from satorineuron import VERSION

        nostr_pubkey = None
        relay_url = None
        try:
            startup = get_startup()
            if startup and hasattr(startup, 'nostrPubkey'):
                nostr_pubkey = startup.nostrPubkey
            if startup and hasattr(startup, 'server') and startup.server:
                relay_url = getattr(startup.server, 'relayUrl', None)
        except Exception as e:
            logger.warning(f"Could not get nostr/relay info: {e}")

        return render_template(
            'dashboard.html',
            version=VERSION,
            eth_wallet_address=None,
            nostr_pubkey=nostr_pubkey,
            relay_url=relay_url,
            page_mode='p2p')

    @app.route('/stake')
    @login_required
    def stake_management():
        """Stake management page for pool staking and pool management."""
        from satorineuron import VERSION
        return render_template('stake.html', version=VERSION)

    @app.route('/local-predictions')
    @login_required
    def local_predictions():
        """Local predictions page."""
        from satorineuron import VERSION
        return render_template('local_predictions.html', version=VERSION)

    @app.route('/health')
    def health():
        """Health check endpoint - checks API server connectivity."""
        api_url = current_app.config.get('SATORI_API_URL', get_api_url())
        try:
            resp = requests.get(f"{api_url}/health", timeout=5)
            if resp.status_code == 200:
                return jsonify({'status': 'ok', 'api': 'connected'})
        except requests.RequestException:
            pass
        return jsonify({'status': 'ok', 'api': 'disconnected'})

    @app.route('/api/engine/status')
    def get_engine_status():
        """Check AI engine initialization status (no auth required for testing)."""
        startup = get_startup()
        if startup is None:
            return jsonify({
                'initialized': False,
                'error': 'Startup instance not set'
            })

        has_aiengine = hasattr(startup, 'aiengine')
        aiengine_not_none = startup.aiengine is not None if has_aiengine else False
        has_streams = False
        stream_count = 0

        if aiengine_not_none and hasattr(startup.aiengine, 'streamModels'):
            has_streams = bool(startup.aiengine.streamModels)
            stream_count = len(startup.aiengine.streamModels)

        return jsonify({
            'initialized': aiengine_not_none,
            'has_aiengine_attr': has_aiengine,
            'aiengine_not_none': aiengine_not_none,
            'has_streams': has_streams,
            'stream_count': stream_count,
            'subscriptions_count': len(startup.subscriptions) if hasattr(startup, 'subscriptions') and startup.subscriptions else 0,
            'publications_count': len(startup.publications) if hasattr(startup, 'publications') and startup.publications else 0
        })

    @app.route('/api/system/stats', methods=['GET'])
    @login_required
    def get_system_stats():
        """Return host and process metrics available on current neuron server."""
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)
            payload = {
                'timestamp_utc': now_utc.isoformat(),
                'host': {
                    'hostname': socket.gethostname(),
                    'platform': platform.platform(),
                    'system': platform.system(),
                    'release': platform.release(),
                    'machine': platform.machine(),
                    'python_version': platform.python_version(),
                },
                'uptime': {
                    'system_seconds': None,
                    'system_started_at_utc': None,
                    'process_seconds': None,
                },
                'cpu': {
                    'logical_cores': os.cpu_count(),
                    'usage_percent': None,
                    'load_average_1_5_15': None,
                },
                'memory': {
                    'virtual': None,
                    'swap': None,
                },
                'disk': {
                    'root': None,
                },
                'network': {
                    'io': None,
                },
                'process': {
                    'pid': os.getpid(),
                    'thread_count': None,
                    'cpu_percent': None,
                    'memory_rss_bytes': None,
                    'memory_vms_bytes': None,
                    'open_file_count': None,
                    'create_time_utc': None,
                },
            }

            try:
                import psutil
            except Exception:
                psutil = None

            if psutil is not None:
                boot_ts = psutil.boot_time()
                payload['uptime']['system_seconds'] = max(0.0, time.time() - boot_ts)
                payload['uptime']['system_started_at_utc'] = datetime.datetime.fromtimestamp(
                    boot_ts, tz=datetime.timezone.utc
                ).isoformat()

                payload['cpu']['usage_percent'] = psutil.cpu_percent(interval=0.1)
                try:
                    payload['cpu']['load_average_1_5_15'] = list(os.getloadavg())
                except Exception:
                    payload['cpu']['load_average_1_5_15'] = None

                vm = psutil.virtual_memory()
                payload['memory']['virtual'] = {
                    'total_bytes': vm.total,
                    'available_bytes': vm.available,
                    'used_bytes': vm.used,
                    'usage_percent': vm.percent,
                }
                sw = psutil.swap_memory()
                payload['memory']['swap'] = {
                    'total_bytes': sw.total,
                    'used_bytes': sw.used,
                    'free_bytes': sw.free,
                    'usage_percent': sw.percent,
                }

                root_path = os.path.abspath(os.sep)
                du = psutil.disk_usage(root_path)
                payload['disk']['root'] = {
                    'path': root_path,
                    'total_bytes': du.total,
                    'used_bytes': du.used,
                    'free_bytes': du.free,
                    'usage_percent': du.percent,
                }

                nio = psutil.net_io_counters()
                payload['network']['io'] = {
                    'bytes_sent': nio.bytes_sent,
                    'bytes_recv': nio.bytes_recv,
                    'packets_sent': nio.packets_sent,
                    'packets_recv': nio.packets_recv,
                    'errin': nio.errin,
                    'errout': nio.errout,
                    'dropin': nio.dropin,
                    'dropout': nio.dropout,
                }

                proc = psutil.Process(os.getpid())
                with proc.oneshot():
                    proc_create_ts = proc.create_time()
                    payload['uptime']['process_seconds'] = max(0.0, time.time() - proc_create_ts)
                    payload['process']['thread_count'] = proc.num_threads()
                    payload['process']['cpu_percent'] = proc.cpu_percent(interval=0.05)
                    mem_info = proc.memory_info()
                    payload['process']['memory_rss_bytes'] = mem_info.rss
                    payload['process']['memory_vms_bytes'] = mem_info.vms
                    payload['process']['create_time_utc'] = datetime.datetime.fromtimestamp(
                        proc_create_ts, tz=datetime.timezone.utc
                    ).isoformat()
                    try:
                        payload['process']['open_file_count'] = len(proc.open_files())
                    except Exception:
                        payload['process']['open_file_count'] = None
            else:
                payload['warning'] = 'psutil not available; returning limited stats'
                payload['uptime']['process_seconds'] = max(0.0, time.time() - _process_started_at)
                try:
                    payload['cpu']['load_average_1_5_15'] = list(os.getloadavg())
                except Exception:
                    payload['cpu']['load_average_1_5_15'] = None

            return jsonify(payload)
        except Exception as e:
            logger.error(f"Failed to collect system stats: {e}")
            return jsonify({'error': str(e)}), 500

    # API Proxy Routes
    def get_auth_headers():
        """Get authentication headers for API requests using JWT.

        This implements JWT authentication:
        1. Check if we have a valid access token in session
        2. If not or expired, login with wallet signature to get JWT tokens
        3. If expiring soon, refresh the access token
        4. Return Authorization header with Bearer token
        """
        from datetime import datetime, timedelta
        import time

        wallet_manager = get_or_create_session_vault()
        if not wallet_manager or not wallet_manager.wallet:
            return None

        wallet = wallet_manager.wallet
        if not hasattr(wallet, 'pubkey') or not hasattr(wallet, 'sign'):
            logger.warning("Wallet missing pubkey or sign method")
            return None

        api_url = current_app.config.get('SATORI_API_URL', get_api_url())

        # Use lock to prevent concurrent login attempts
        with _auth_lock:
            try:
                # Check if we have a valid JWT token in session
                access_token = session.get('access_token')
                refresh_token = session.get('refresh_token')
                token_expiry = session.get('token_expiry')

                # Convert token_expiry back to datetime if it exists
                if token_expiry:
                    token_expiry = datetime.fromisoformat(token_expiry)

                now = datetime.now()

                # If no token or expired, perform JWT login
                if not access_token or not token_expiry or now >= token_expiry:
                    logger.info("JWT token missing or expired, performing login")

                    # Generate challenge (timestamp)
                    challenge = str(time.time())

                    # Sign with wallet
                    signature = wallet.sign(message=challenge)
                    if isinstance(signature, bytes):
                        signature = signature.decode('utf-8')

                    # Call JWT login endpoint
                    resp = requests.post(
                        f"{api_url}/api/v1/auth/login",
                        headers={
                            'wallet-pubkey': wallet.pubkey,
                            'message': challenge,
                            'signature': signature
                        },
                        timeout=10
                    )

                    if resp.status_code != 200:
                        logger.warning(f"JWT login failed: {resp.text}")
                        return None

                    data = resp.json()
                    access_token = data['access_token']
                    refresh_token = data['refresh_token']
                    expires_in = data['expires_in']

                    # Store tokens in session
                    session['access_token'] = access_token
                    session['refresh_token'] = refresh_token
                    session['token_expiry'] = (now + timedelta(seconds=expires_in)).isoformat()

                    logger.info("JWT login successful")

                # Token expiring soon (within 5 minutes)? Refresh it
                elif now >= (token_expiry - timedelta(minutes=5)) and refresh_token:
                    logger.info("JWT token expiring soon, refreshing")

                    try:
                        resp = requests.post(
                            f"{api_url}/api/v1/auth/refresh",
                            headers={'Authorization': f'Bearer {refresh_token}'},
                            timeout=10
                        )

                        if resp.status_code == 200:
                            data = resp.json()
                            access_token = data['access_token']
                            expires_in = data['expires_in']

                            # Update session with new token
                            session['access_token'] = access_token
                            session['token_expiry'] = (now + timedelta(seconds=expires_in)).isoformat()

                            logger.info("JWT token refreshed successfully")
                        else:
                            logger.warning(f"JWT refresh failed: {resp.text}")
                    except Exception as e:
                        logger.warning(f"JWT refresh failed: {e}")

                # Return JWT Bearer token header
                return {
                    'Authorization': f'Bearer {access_token}'
                }

            except Exception as e:
                logger.warning(f"JWT auth header generation failed: {e}")
                return None

    def proxy_api(endpoint, method='GET', data=None, authenticated=True):
        """Proxy requests to the Satori API server."""
        api_url = current_app.config.get('SATORI_API_URL', get_api_url())
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
                # Forward query parameters from the incoming request
                resp = requests.get(url, params=request.args, headers=headers, timeout=10)
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
        """
        Get or set reward address using local config (synced with server by start.py).

        GET: Reads from local config (fast, no server call)
        POST: Updates both local config and server (via setRewardAddress)
        """
        startup = get_startup()

        if request.method == 'POST':
            # Get the new reward address from request
            data = request.get_json()
            if not data or 'reward_address' not in data:
                return jsonify({'error': 'Missing reward_address'}), 400

            new_address = data['reward_address']

            # Update both local config and server using setRewardAddress
            if startup:
                try:
                    success = startup.setRewardAddress(address=new_address, globally=True)
                    if success:
                        return jsonify({'success': True, 'reward_address': new_address})
                    else:
                        return jsonify({'error': 'Failed to set reward address'}), 500
                except Exception as e:
                    logger.error(f"Error setting reward address: {e}")
                    return jsonify({'error': str(e)}), 500
            else:
                # Fallback to proxy if startup not available
                return proxy_api('/peer/reward-address', 'POST', request.json)

        # GET request - read from local config
        if startup and hasattr(startup, 'configRewardAddress'):
            return jsonify({'reward_address': startup.configRewardAddress or ''})
        else:
            # Fallback to proxy if startup not available
            return proxy_api('/peer/reward-address')

    @app.route('/api/lender/status')
    @login_required
    def api_lender_status():
        """Proxy lender status request (public endpoint, no auth required)."""
        return proxy_api('/lender/status', authenticated=False)

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

    @app.route('/api/pool/lenders', methods=['GET'])
    @login_required
    def api_pool_lenders():
        """Get list of lenders for authenticated user's pool."""
        return proxy_api('/pool/lenders', 'GET')

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

    @app.route('/api/wallet/send-from-wallet', methods=['POST'])
    @login_required
    def api_wallet_send_from_wallet():
        """Send SATORI tokens from wallet (identity wallet)."""
        wallet_manager = get_or_create_session_vault()
        if not wallet_manager or not wallet_manager.wallet:
            return jsonify({'error': 'Wallet not initialized'}), 500

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
            wallet = wallet_manager.wallet
            # Get ready to send
            wallet.get()
            wallet.getReadyToSend()

            # typicalNeuronTransaction handles both direct (has EVR) and
            # indirect (no EVR, uses Mundo) paths, including sweep
            result = wallet.typicalNeuronTransaction(
                amount=amount if not sweep else 0,
                address=address,
                sweep=sweep,
                requestSimplePartialFn=_mundoRequestSimplePartial,
                broadcastSimplePartialFn=_mundoBroadcastSimplePartial)

            if hasattr(result, 'success') and hasattr(result, 'msg'):
                if result.success and result.msg and len(result.msg) == 64:
                    return jsonify({'success': True, 'txid': result.msg})
                else:
                    error_msg = result.msg or 'Transaction failed'
                    return jsonify({'error': error_msg}), 500
            else:
                return jsonify({'error': 'Unexpected transaction result', 'details': str(result)}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

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

            # typicalNeuronTransaction handles both direct (has EVR) and
            # indirect (no EVR, uses Mundo) paths, including sweep
            result = vault.typicalNeuronTransaction(
                amount=amount if not sweep else 0,
                address=address,
                sweep=sweep,
                requestSimplePartialFn=_mundoRequestSimplePartial,
                broadcastSimplePartialFn=_mundoBroadcastSimplePartial)

            if hasattr(result, 'success') and hasattr(result, 'msg'):
                if result.success and result.msg and len(result.msg) == 64:
                    return jsonify({'success': True, 'txid': result.msg})
                else:
                    error_msg = result.msg or 'Transaction failed'
                    return jsonify({'error': error_msg}), 500
            else:
                return jsonify({'error': 'Unexpected transaction result', 'details': str(result)}), 500
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    @app.route('/api/lender/pay', methods=['POST'])
    @login_required
    def api_lender_pay():
        """Pay lenders from audit - simple single-transaction approach."""
        try:
            # Get wallet manager
            wallet_manager = get_or_create_session_vault()
            if not wallet_manager:
                return jsonify({'error': 'Wallet not initialized'}), 500

            vault = wallet_manager.vault

            # Check vault is unlocked
            if not vault.isDecrypted:
                return jsonify({'error': 'Vault is locked'}), 401

            # Get payments from request
            data = request.json
            if not data:
                return jsonify({'error': 'No data provided'}), 400

            payments = data.get('payments', {})

            if not payments:
                return jsonify({'error': 'No payments provided'}), 400

            # Check limit (blockchain max is 1000)
            if len(payments) > 1000:
                return jsonify({
                    'error': f'Too many lenders ({len(payments)}). Maximum is 1000.'
                }), 400

            # Calculate total
            total = sum(payments.values())

            # Check balance
            vault.get()
            if vault.balance.amount < total:
                return jsonify({
                    'error': f'Insufficient balance: have {vault.balance.amount}, need {total}'
                }), 400

            # Prepare and send
            vault.getReadyToSend()
            txhash = vault.satoriDistribution(
                amountByAddress=payments,
                memo='Lender rewards',
                broadcast=True
            )

            # Validate txhash
            if txhash and isinstance(txhash, str) and len(txhash) == 64:
                return jsonify({
                    'success': True,
                    'txhash': txhash,
                    'lenders_paid': len(payments),
                    'total_sent': total
                })
            else:
                return jsonify({'error': f'Invalid transaction hash: {txhash}'}), 500

        except Exception as e:
            app.logger.error(f"Error in lender payment: {str(e)}")
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
            # Ensure electrumx connection with retry
            logger.info("Attempting to connect to ElectrumX...")
            connected = False
            if hasattr(wallet_manager, 'connect'):
                for attempt in range(5):
                    connected = wallet_manager.connect()
                    logger.info(f"ElectrumX connection attempt {attempt + 1}: {connected}")
                    if connected:
                        break
                    time.sleep(2)  # Give more time for connection to establish
                if not connected:
                    logger.warning("Failed to connect to ElectrumX after retries")
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
                    # Retry if connection not ready yet
                    for _ in range(3):
                        if wallet.electrumx and wallet.electrumx.connected():
                            wallet.getBalances()
                            break
                        time.sleep(1)
                    else:
                        wallet.getBalances()  # Final attempt
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
                    # Retry if connection not ready yet
                    for _ in range(3):
                        if vault.electrumx and vault.electrumx.connected():
                            vault.getBalances()
                            break
                        time.sleep(1)
                    else:
                        vault.getBalances()  # Final attempt
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

    # AI Engine Training Delay Control
    @app.route('/api/engine/training-delay', methods=['GET'])
    @login_required
    def get_training_delay():
        """Get current AI engine training delay setting.

        Returns:
            JSON with delay_seconds (int)
        """
        try:
            from satorineuron import config

            # Default to 10 minutes if not set
            delay = config.get().get('training_delay', 600)

            return jsonify({
                'delay_seconds': delay
            })
        except Exception as e:
            logger.error(f"Error getting training delay: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/engine/training-delay', methods=['POST'])
    @login_required
    def set_training_delay():
        """Set AI engine training delay.

        Request Body:
            {
                "delay_seconds": int  // 0 to 86400
            }

        Returns:
            JSON with updated delay_seconds
        """
        try:
            from satorineuron import config
            from start import getStart

            data = request.get_json()
            delay_seconds = data.get('delay_seconds')

            # Validate
            if delay_seconds is None:
                return jsonify({'error': 'delay_seconds required'}), 400

            delay_seconds = int(delay_seconds)

            if delay_seconds < 0 or delay_seconds > 86400:
                return jsonify({'error': 'delay_seconds must be between 0 and 86400'}), 400

            # Save to config
            config.add(data={'training_delay': delay_seconds})

            # Update running engine instances
            try:
                startup = getStart()
                if hasattr(startup, 'aiengine') and startup.aiengine is not None:
                    for streamUuid, streamModel in startup.aiengine.streamModels.items():
                        streamModel.trainingDelay = delay_seconds
                        logger.info(f"Updated training delay for stream {streamUuid}: {delay_seconds}s")
            except Exception as e:
                # Log but don't fail if engine isn't running
                logger.warning(f"Could not update running engine instances: {e}")

            return jsonify({
                'delay_seconds': delay_seconds,
                'message': 'Training delay updated successfully'
            })

        except Exception as e:
            logger.error(f"Error setting training delay: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/engine/streams', methods=['GET'])
    @login_required
    def get_engine_streams():
        """List streams for local predictions (active + stored prediction tables)."""
        try:
            startup = get_startup()
            if startup is None or not hasattr(startup, 'aiengine') or startup.aiengine is None:
                return jsonify({'error': 'AI engine not initialized'}), 503

            stream_models = getattr(startup.aiengine, 'streamModels', {}) or {}
            storage = getattr(startup.aiengine, 'storage', None)
            streams = []
            known_prediction_ids = set()
            pred_to_obs_meta = {}

            def prediction_uuid_for_name(stream_name: str) -> str:
                if not stream_name:
                    return None
                seed = f"central-lite:satori:{stream_name}_pred:"
                return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

            if storage is not None:
                try:
                    for row in storage.db.getAllStreams():
                        name = row.get('name')
                        obs_uuid = row.get('uuid')
                        pred_uuid = prediction_uuid_for_name(name)
                        if pred_uuid:
                            pred_to_obs_meta[pred_uuid] = {
                                'observation_stream_uuid': obs_uuid,
                                'stream_name': name,
                                'source': row.get('secondary'),
                                'author': row.get('author'),
                                'target': row.get('target'),
                            }
                except Exception:
                    pred_to_obs_meta = {}

            for obs_uuid, model in stream_models.items():
                pred_uuid = getattr(model, 'predictionStreamUuid', None)
                if pred_uuid:
                    known_prediction_ids.add(pred_uuid)

                sub_stream = getattr(model, 'subscriptionStream', None)
                stream_id = getattr(sub_stream, 'streamId', None) if sub_stream else None
                stream_name = getattr(stream_id, 'stream', None) if stream_id else None
                source = getattr(stream_id, 'source', None) if stream_id else None
                author = getattr(stream_id, 'author', None) if stream_id else None
                target = getattr(stream_id, 'target', None) if stream_id else None
                if not stream_name:
                    stream_name = f"stream-{obs_uuid[:8]}"

                streams.append({
                    'stream_uuid': pred_uuid or obs_uuid,
                    'prediction_stream_uuid': pred_uuid or obs_uuid,
                    'observation_stream_uuid': obs_uuid,
                    'stream_name': stream_name,
                    'source': source,
                    'author': author,
                    'target': target,
                    'stream_type': 'active_model',
                })

            predictions_24h_total = 0
            if storage is not None:
                import pandas as pd
                now_utc = pd.Timestamp.utcnow()
                # Add stored prediction tables from local DB.
                for table_name in storage.listAllStreams():
                    if table_name in ('streams', 'sqlite_sequence'):
                        continue
                    if table_name in known_prediction_ids:
                        continue
                    if storage.getStreamRowCount(table_name) <= 0:
                        continue

                    table_df = storage.getPredictions(table_name)
                    if table_df.empty or 'provider' not in table_df.columns:
                        continue
                    providers = {str(p).lower() for p in table_df['provider'].dropna().tolist()}
                    if 'engine' not in providers:
                        continue

                    meta = pred_to_obs_meta.get(table_name, {})
                    name = meta.get('stream_name') or f"prediction-{table_name[:8]}"

                    streams.append({
                        'stream_uuid': table_name,
                        'prediction_stream_uuid': table_name,
                        'observation_stream_uuid': meta.get('observation_stream_uuid'),
                        'stream_name': name,
                        'source': (meta.get('source') or 'local-db'),
                        'author': meta.get('author'),
                        'target': meta.get('target'),
                        'stream_type': 'stored_prediction',
                    })

                # Compute 24h prediction count.
                for item in streams:
                    pred_uuid = item.get('prediction_stream_uuid') or item.get('stream_uuid')
                    if not pred_uuid:
                        continue
                    pdf = storage.getPredictions(pred_uuid)
                    if pdf.empty:
                        continue
                    for ts in pdf.index:
                        try:
                            ts_dt = pd.to_datetime(ts, utc=True)
                        except Exception:
                            continue
                        if ts_dt >= now_utc - pd.Timedelta(hours=24):
                            predictions_24h_total += 1

            return jsonify({
                'streams': sorted(streams, key=lambda x: (x.get('stream_name') or '').lower()),
                'count': len(streams),
                'active_model_count': len(stream_models),
                'predictions_24h_total': predictions_24h_total,
            })
        except Exception as e:
            logger.error(f"Error getting engine streams: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/engine/performance', methods=['GET'])
    @login_required
    def get_engine_performance():
        """Get engine performance metrics (predictions vs observations).

        Returns recent observations and predictions with accuracy calculations.

        Returns:
            JSON with:
            - observations: [{ts, value}, ...]
            - predictions: [{ts, value}, ...]
            - accuracy: [{ts, error, abs_error}, ...]
            - stats: {avg_error, avg_abs_error, accuracy_pct}
        """
        try:
            # Use the stored startup instance instead of importing
            startup = get_startup()
            if startup is None or not hasattr(startup, 'aiengine') or startup.aiengine is None:
                logger.warning("AI engine not initialized")
                return jsonify({'error': 'AI engine not initialized'}), 503

            stream_models = getattr(startup.aiengine, 'streamModels', {}) or {}
            storage = getattr(startup.aiengine, 'storage', None)
            if not stream_models and storage is None:
                return jsonify({'error': 'No streams configured'}), 404

            try:
                limit = int(request.args.get('limit', 0))
            except (TypeError, ValueError):
                limit = 0
            limit = max(0, min(limit, 50000))

            raw_days = (request.args.get('days', '10') or '10').strip().lower()
            if raw_days == 'all':
                window_days = None
            else:
                try:
                    parsed_days = int(raw_days)
                except (TypeError, ValueError):
                    parsed_days = 10
                window_days = parsed_days if parsed_days > 0 else 10

            requested_stream = request.args.get('stream_uuid')
            selected_model = None
            obs_uuid = None
            pred_uuid = None

            def prediction_uuid_for_name(stream_name: str) -> str:
                if not stream_name:
                    return None
                seed = f"central-lite:satori:{stream_name}_pred:"
                return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

            pred_to_obs = {}
            if storage is not None:
                try:
                    for row in storage.db.getAllStreams():
                        n = row.get('name')
                        p = prediction_uuid_for_name(n)
                        if p:
                            pred_to_obs[p] = row.get('uuid')
                except Exception:
                    pred_to_obs = {}

            if requested_stream:
                for model_obs_uuid, model in stream_models.items():
                    model_pred_uuid = getattr(model, 'predictionStreamUuid', None)
                    if requested_stream == model_obs_uuid or requested_stream == model_pred_uuid:
                        selected_model = model
                        obs_uuid = model_obs_uuid
                        pred_uuid = model_pred_uuid
                        break
                if selected_model is None:
                    pred_uuid = requested_stream
                    if storage is None or pred_uuid not in storage.listAllStreams():
                        return jsonify({'error': f'Stream not found: {requested_stream}'}), 404
            else:
                if not stream_models:
                    return jsonify({'error': 'No active streams configured'}), 404
                obs_uuid = list(stream_models.keys())[0]
                selected_model = stream_models[obs_uuid]
                pred_uuid = getattr(selected_model, 'predictionStreamUuid', None)

            if selected_model is not None:
                if not hasattr(selected_model, 'storage') or selected_model.storage is None:
                    return jsonify({'error': 'Storage not initialized'}), 503
                storage = selected_model.storage

            if storage is None:
                return jsonify({'error': 'Storage not initialized'}), 503

            # If we only have a prediction stream from local DB, try to map to active model observation.
            if obs_uuid is None and pred_uuid is not None:
                for model_obs_uuid, model in stream_models.items():
                    if getattr(model, 'predictionStreamUuid', None) == pred_uuid:
                        obs_uuid = model_obs_uuid
                        break
            if obs_uuid is None and pred_uuid in pred_to_obs:
                obs_uuid = pred_to_obs.get(pred_uuid)

            import pandas as pd

            def normalize_ts_column(frame: pd.DataFrame) -> pd.Series:
                ts_values = frame['ts']
                parsed = pd.to_datetime(ts_values, errors='coerce', utc=True)
                numeric = pd.to_numeric(ts_values, errors='coerce')
                numeric_mask = numeric.notna()
                if numeric_mask.any():
                    median_abs = float(numeric[numeric_mask].abs().median())
                    if median_abs < 1e11:
                        unit = 's'
                    elif median_abs < 1e14:
                        unit = 'ms'
                    else:
                        unit = 'ns'
                    parsed.loc[numeric_mask] = pd.to_datetime(
                        numeric[numeric_mask], errors='coerce', utc=True, unit=unit
                    )
                return parsed

            def apply_window(frame: pd.DataFrame) -> pd.DataFrame:
                frame = frame.reset_index()
                if window_days is not None:
                    ts_parsed = normalize_ts_column(frame)
                    cutoff = pd.Timestamp.utcnow() - pd.Timedelta(days=window_days)
                    frame = frame[ts_parsed >= cutoff].copy()
                if limit > 0:
                    frame = frame.tail(limit)
                return frame.reset_index(drop=True)

            obs_df_raw = storage.getStreamData(obs_uuid) if obs_uuid else None
            obs_df = apply_window(obs_df_raw) if obs_df_raw is not None and not obs_df_raw.empty else None
            observations = [
                {'ts': str(row['ts']), 'value': float(row['value'])}
                for _, row in obs_df.iterrows()
            ] if obs_df is not None else []

            pred_df = storage.getPredictions(pred_uuid) if pred_uuid else None
            if pred_df is None or pred_df.empty:
                return jsonify({
                    'stream_uuid': pred_uuid or obs_uuid,
                    'observations': observations,
                    'predictions': [],
                    'accuracy': [],
                    'stats': {}
                })

            pred_df = apply_window(pred_df)
            predictions = [
                {'ts': str(row['ts']), 'value': float(row['value'])}
                for _, row in pred_df.iterrows()
            ]

            # Calculate accuracy: match each prediction to the next observation
            accuracy_data = []
            if obs_df is None or obs_df.empty:
                return jsonify({
                    'stream_uuid': pred_uuid or obs_uuid,
                    'observations': observations,
                    'predictions': predictions,
                    'accuracy': [],
                    'stats': {}
                })

            for idx, pred_row in pred_df.iterrows():
                pred_ts = pred_row['ts']
                pred_value = float(pred_row['value'])

                # Find next observation after this prediction
                # Ensure timestamp comparison works by converting pred_ts to same type
                try:
                    # Convert pred_ts to match obs_df['ts'] dtype
                    if pd.api.types.is_datetime64_any_dtype(obs_df['ts']):
                        # Observations are datetime - convert pred_ts to datetime
                        pred_ts_compare = pd.to_datetime(pred_ts, utc=True)
                    elif pd.api.types.is_numeric_dtype(obs_df['ts']):
                        # Observations are numeric (Unix timestamps)
                        # Try to convert pred_ts to numeric
                        try:
                            # First try direct conversion (if pred_ts is already numeric)
                            pred_ts_compare = float(pred_ts)
                        except (ValueError, TypeError):
                            # If that fails, pred_ts might be a datetime string
                            # Convert to datetime then to Unix timestamp
                            pred_ts_dt = pd.to_datetime(pred_ts)
                            pred_ts_compare = pred_ts_dt.timestamp()
                    else:
                        # Observations are object/string - keep as-is
                        pred_ts_compare = pred_ts
                    next_obs = obs_df[obs_df['ts'] > pred_ts_compare]
                except (ValueError, TypeError) as e:
                    # If conversion fails, skip this prediction
                    logger.warning(f"Timestamp comparison failed for prediction {idx}: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Unexpected error in accuracy calculation at {idx}: {e}")
                    continue
                if not next_obs.empty:
                    obs_value = float(next_obs.iloc[0]['value'])
                    error = pred_value - obs_value
                    abs_error = abs(error)

                    accuracy_data.append({
                        'ts': str(pred_ts),
                        'error': error,
                        'abs_error': abs_error,
                        'predicted': pred_value,
                        'actual': obs_value
                    })

            # Calculate statistics
            stats = {}
            if accuracy_data:
                errors = [d['error'] for d in accuracy_data]
                abs_errors = [d['abs_error'] for d in accuracy_data]
                actuals = [d['actual'] for d in accuracy_data]

                avg_error = sum(errors) / len(errors)
                avg_abs_error = sum(abs_errors) / len(abs_errors)
                avg_actual = sum(actuals) / len(actuals) if actuals else 1

                # Accuracy percentage (100% - average % error)
                avg_pct_error = (avg_abs_error / avg_actual * 100) if avg_actual != 0 else 0
                accuracy_pct = max(0, 100 - avg_pct_error)

                stats = {
                    'avg_error': round(avg_error, 4),
                    'avg_abs_error': round(avg_abs_error, 4),
                    'accuracy_pct': round(accuracy_pct, 2)
                }

            return jsonify({
                'stream_uuid': pred_uuid or obs_uuid,
                'observations': observations,
                'predictions': predictions,
                'accuracy': accuracy_data,
                'stats': stats,
                'window_days': window_days
            })

        except Exception as e:
            logger.error(f"Error getting engine performance: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return jsonify({'error': str(e)}), 500

    @app.route('/api/wallet/import', methods=['POST'])
    @login_required
    def api_wallet_import():
        """Import wallet from uploaded files with automatic rollback on failure.

        Security: Requires vault to be unlocked (login_required decorator)
        Process:
        1. Validate uploaded files
        2. Create timestamped backup of existing wallet directory
        3. Save uploaded files to temporary directory
        4. Validate YAML structure
        5. Move files to wallet directory
        6. Trigger container restart (critical for safety)

        Rollback: If ANY step fails, automatically restores from backup

        Expected files in upload:
        - wallet.yaml (required)
        - vault.yaml (required)
        - wallet.yaml.bak (optional)
        - vault.yaml.bak (optional)
        """
        import tempfile
        import shutil
        import datetime
        from pathlib import Path
        import yaml

        try:
            # 1. Validate request has files
            if 'files' not in request.files:
                return jsonify({'error': 'No files uploaded'}), 400

            files = request.files.getlist('files')
            if not files:
                return jsonify({'error': 'No files selected'}), 400

            # 2. Validate required files are present
            file_names = [f.filename for f in files if f.filename]
            required_files = {'wallet.yaml', 'vault.yaml'}
            uploaded_files = set(file_names)

            if not required_files.issubset(uploaded_files):
                missing = required_files - uploaded_files
                return jsonify({
                    'error': f'Missing required files: {", ".join(missing)}'
                }), 400

            # 3. Validate file names (security: prevent directory traversal)
            allowed_files = {'wallet.yaml', 'vault.yaml', 'wallet.yaml.bak', 'vault.yaml.bak'}
            for name in file_names:
                if name not in allowed_files:
                    return jsonify({
                        'error': f'Invalid file: {name}. Only wallet files allowed.'
                    }), 400

            # 4. Get wallet directory path
            from satorineuron import config
            wallet_dir = Path(config.walletPath())

            # 5. Create timestamped backup of wallet files inside wallet directory
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_dir = wallet_dir / f'backup_{timestamp}'

            try:
                if wallet_dir.exists():
                    # Create backup directory inside wallet folder
                    backup_dir.mkdir(parents=True, exist_ok=True)

                    # Copy all wallet files to backup (excluding other backup folders)
                    for item in wallet_dir.iterdir():
                        if item.is_file():
                            shutil.copy2(item, backup_dir / item.name)

                    logger.info(f"Created backup at {backup_dir}")
                else:
                    logger.warning(f"Wallet directory doesn't exist yet: {wallet_dir}")
                    wallet_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.error(f"Backup failed: {e}")
                return jsonify({
                    'error': f'Failed to create backup: {str(e)}'
                }), 500

            # 6. Save uploaded files to temporary location first
            temp_dir = Path(tempfile.mkdtemp())
            try:
                # Save files to temp
                for file in files:
                    if file.filename:
                        file_path = temp_dir / file.filename
                        file.save(str(file_path))
                        logger.info(f"Saved {file.filename} to temp: {file_path}")

                # 7. Validate file contents (basic YAML check)
                for file_name in ['wallet.yaml', 'vault.yaml']:
                    file_path = temp_dir / file_name
                    try:
                        with open(file_path, 'r') as f:
                            yaml.safe_load(f)
                        logger.info(f"Validated {file_name}")
                    except Exception as e:
                        # Validation failed - cleanup and rollback
                        shutil.rmtree(temp_dir)
                        logger.error(f"Invalid {file_name}: {e}")

                        # Rollback: This shouldn't be needed as we haven't modified wallet_dir yet
                        # but include for safety
                        if backup_dir.exists():
                            logger.info("Validation failed before any changes - no rollback needed")

                        return jsonify({
                            'error': f'Invalid {file_name}: {str(e)}',
                            'rolled_back': False
                        }), 400

                # 8. Files validated - move to wallet directory
                for file_name in file_names:
                    src = temp_dir / file_name
                    dst = wallet_dir / file_name
                    shutil.move(str(src), str(dst))
                    logger.info(f"Moved {file_name} to wallet directory")

                logger.info(f"Wallet import: saved {len(file_names)} files")

                # 8.5. Clear reward address from config since wallet changed
                try:
                    # Read config, remove reward address, and write back
                    config_data = config.get()
                    logger.info(f"Current config before clearing reward address: {list(config_data.keys())}")

                    if 'reward address' in config_data:
                        old_address = config_data['reward address']
                        del config_data['reward address']
                        # Use put to write the entire config back (this will remove the key)
                        config.put(data=config_data)
                        logger.info(f"Cleared old reward address '{old_address}' from config")

                        # Verify it was removed
                        verify_config = config.get()
                        if 'reward address' in verify_config:
                            logger.error("Failed to remove reward address from config!")
                        else:
                            logger.info("Verified: reward address successfully removed from config")
                    else:
                        logger.info("No reward address found in config to clear")
                except Exception as config_error:
                    logger.error(f"Error clearing reward address from config: {config_error}")
                    import traceback
                    logger.error(traceback.format_exc())
                    # Don't fail the import for this - it's not critical

            except Exception as e:
                # Error during file operations - ROLLBACK
                logger.error(f"Import failed during file operations: {e}. Rolling back...")

                # Delete any partial changes in wallet_dir
                for file_name in file_names:
                    try:
                        (wallet_dir / file_name).unlink(missing_ok=True)
                    except Exception as cleanup_error:
                        logger.warning(f"Cleanup error for {file_name}: {cleanup_error}")

                # Restore from backup
                if backup_dir.exists():
                    try:
                        for item in backup_dir.iterdir():
                            if item.is_file():
                                shutil.copy2(item, wallet_dir / item.name)
                        logger.info("Rollback completed. Old wallet restored.")
                    except Exception as rollback_error:
                        logger.error(f"Rollback failed: {rollback_error}")
                        return jsonify({
                            'error': f'Import failed AND rollback failed: {str(e)}. Backup at: {backup_dir}',
                            'rolled_back': False
                        }), 500

                return jsonify({
                    'error': str(e),
                    'rolled_back': True
                }), 500

            finally:
                # Cleanup temp directory
                if temp_dir.exists():
                    shutil.rmtree(temp_dir)

            # 9. Schedule container restart (happens after response sent)
            def delayed_restart():
                import time
                time.sleep(2)  # Give response time to be sent
                startup = get_startup()
                if startup:
                    logger.info("Triggering restart after wallet import")
                    startup.triggerRestart()
                else:
                    logger.error("Cannot restart - startup instance not available")

            import threading
            threading.Thread(target=delayed_restart, daemon=True).start()

            return jsonify({
                'success': True,
                'message': 'Wallet imported successfully. Container restarting...',
                'backup_location': str(backup_dir)
            })

        except Exception as e:
            logger.error(f"Wallet import error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return jsonify({'error': str(e)}), 500

    @app.route('/api/relay', methods=['POST'])
    @login_required
    def api_relay_register():
        """Register relay URL with central server for NIP-11 verification."""
        startup = get_startup()
        if not startup or not hasattr(startup, 'server') or not startup.server:
            return jsonify({'error': 'Server connection not initialized'}), 503

        data = request.get_json()
        if not data or 'relay_url' not in data:
            return jsonify({'error': 'Missing relay_url'}), 400

        relay_url = data['relay_url'].strip()
        if not relay_url.startswith(('wss://', 'ws://')):
            return jsonify({'error': 'Relay URL must start with wss:// or ws://'}), 400

        try:
            result = startup.server.registerRelay(relay_url)
            if isinstance(result, requests.Response):
                if result.status_code == 200:
                    return jsonify(result.json())
                else:
                    return jsonify({'error': result.text}), result.status_code
            return jsonify({'success': True, 'relay_url': relay_url})
        except Exception as e:
            logger.error(f"Relay registration error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/network/streams', methods=['GET'])
    @login_required
    def api_network_streams():
        """Return cached discovered streams (no global discovery trigger)."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        connected = len(startup._networkClients) > 0
        streams = []
        for s in startup.networkStreams:
            s_copy = dict(s)
            s_copy['subscribed'] = startup.networkDB.is_subscribed(
                s['stream_name'], s['nostr_pubkey'])
            streams.append(s_copy)
        return jsonify({
            'connected': connected,
            'streams': streams,
        })

    @app.route('/api/network/streams/relay', methods=['GET'])
    @login_required
    def api_network_streams_relay():
        """Discover streams on a single relay."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        relay_url = request.args.get('url')
        if not relay_url:
            return jsonify({'error': 'Missing url parameter'}), 400
        streams = startup.discoverRelaySync(relay_url)
        for s in streams:
            s['subscribed'] = startup.networkDB.is_subscribed(
                s['stream_name'], s['nostr_pubkey'])
        return jsonify({'streams': streams})

    @app.route('/api/network/subscribe', methods=['POST'])
    @login_required
    def api_network_subscribe():
        """Subscribe to a datastream."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'stream_name' not in data or 'nostr_pubkey' not in data:
            return jsonify({'error': 'Missing stream_name or nostr_pubkey'}), 400
        relay_url = data.get('relay_url', '')
        startup.networkDB.subscribe(data, relay_url)
        return jsonify({'success': True})

    @app.route('/api/network/unsubscribe', methods=['POST'])
    @login_required
    def api_network_unsubscribe():
        """Unsubscribe from a datastream (soft delete)."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'stream_name' not in data or 'nostr_pubkey' not in data:
            return jsonify({'error': 'Missing stream_name or nostr_pubkey'}), 400
        startup.networkDB.unsubscribe(data['stream_name'], data['nostr_pubkey'])
        return jsonify({'success': True})

    @app.route('/api/network/predict', methods=['POST'])
    @login_required
    def api_network_predict():
        """Start predicting a subscribed stream."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'stream_name' not in data or 'nostr_pubkey' not in data:
            return jsonify({'error': 'Missing stream_name or nostr_pubkey'}), 400
        stream_name = data['stream_name']
        provider_pubkey = data['nostr_pubkey']
        # Look up the subscription for metadata
        subs = startup.networkDB.get_active()
        sub = next((s for s in subs
                     if s['stream_name'] == stream_name
                     and s['provider_pubkey'] == provider_pubkey), None)
        pred_name = stream_name + '_pred'
        startup.networkDB.add_publication(
            stream_name=pred_name,
            name=f"Predictions for {sub.get('name') or stream_name}" if sub else f"Predictions for {stream_name}",
            cadence_seconds=sub.get('cadence_seconds') if sub else None,
            source_stream_name=stream_name,
            source_provider_pubkey=provider_pubkey)
        return jsonify({'success': True, 'publication': pred_name})

    @app.route('/api/network/stop-predict', methods=['POST'])
    @login_required
    def api_network_stop_predict():
        """Stop predicting a subscribed stream."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'stream_name' not in data:
            return jsonify({'error': 'Missing stream_name'}), 400
        pred_name = data['stream_name'] + '_pred'
        startup.networkDB.remove_publication(pred_name)
        return jsonify({'success': True})

    @app.route('/api/network/subscriptions', methods=['GET'])
    @login_required
    def api_network_subscriptions():
        """Return subscriptions. Pass ?all=1 to include inactive."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        if request.args.get('all'):
            subs = startup.networkDB.get_all()
        else:
            subs = startup.networkDB.get_active()
        # Annotate each subscription with prediction status
        for sub in subs:
            sub['predicting'] = startup.networkDB.is_predicting(
                sub['stream_name'], sub['provider_pubkey'])
        return jsonify({'subscriptions': subs})

    @app.route('/api/network/observations', methods=['GET'])
    @login_required
    def api_network_observations():
        """Return recent observations and predictions for a stream."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        stream_name = request.args.get('stream_name')
        provider_pubkey = request.args.get('provider_pubkey')
        if not stream_name or not provider_pubkey:
            return jsonify({'error': 'Missing stream_name or provider_pubkey'}), 400
        limit = int(request.args.get('limit', 50))
        observations = startup.networkDB.get_observations(
            stream_name, provider_pubkey, limit=limit)
        predictions = startup.networkDB.get_predictions(
            stream_name, provider_pubkey, limit=limit)
        return jsonify({
            'observations': observations,
            'predictions': predictions,
        })

    @app.route('/api/network/publications', methods=['GET'])
    @login_required
    def api_network_publications():
        """Return publications. Pass ?all=1 to include inactive."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        if request.args.get('all'):
            pubs = startup.networkDB.get_all_publications()
        else:
            pubs = startup.networkDB.get_active_publications()
        return jsonify({'publications': pubs})

    @app.route('/api/network/data-source/test', methods=['POST'])
    @login_required
    def api_network_data_source_test():
        """Test a data source: fetch URL and run parser."""
        import requests as http_requests
        import json as json_mod
        data = request.get_json()
        url = data.get('url', '').strip()
        method = data.get('method', 'GET').upper()
        headers = None
        if data.get('headers'):
            try:
                headers = json_mod.loads(data['headers'])
            except Exception:
                return jsonify({'error': 'Invalid headers JSON', 'raw': ''})
        parser_type = data.get('parser_type', 'json_path')
        parser_config = data.get('parser_config', '')
        # Fetch
        try:
            if method == 'POST':
                resp = http_requests.post(url, headers=headers, timeout=15)
            else:
                resp = http_requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            raw = resp.text
        except Exception as e:
            return jsonify({'error': f'Fetch failed: {e}', 'raw': ''})
        # Parse
        try:
            if parser_type == 'json_path':
                obj = json_mod.loads(raw)
                for key in parser_config.split('.'):
                    if key.isdigit():
                        obj = obj[int(key)]
                    else:
                        obj = obj[key]
                value = str(obj)
            elif parser_type == 'python':
                local_vars = {'text': raw}
                exec_code = parser_config.strip()
                # Wrap in function if not already
                if 'return ' in exec_code and not exec_code.startswith('def '):
                    exec_code = ('def _parse(text):\n' +
                                 '\n'.join('    ' + l for l in exec_code.split('\n')) +
                                 '\n_result = _parse(text)')
                    exec(exec_code, {}, local_vars)
                    value = str(local_vars.get('_result', ''))
                else:
                    exec(exec_code, {}, local_vars)
                    value = str(local_vars.get('result', local_vars.get('_result', '')))
            else:
                value = ''
                return jsonify({'error': f'Unknown parser type: {parser_type}', 'raw': raw})
        except Exception as e:
            return jsonify({'error': f'Parse failed: {e}', 'raw': raw})
        return jsonify({'value': value, 'raw': raw})

    @app.route('/api/network/data-source', methods=['GET'])
    @login_required
    def api_network_data_source_get():
        """Return a single data source by stream_name."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        stream_name = request.args.get('stream_name')
        if not stream_name:
            return jsonify({'error': 'Missing stream_name'}), 400
        ds = startup.networkDB.get_data_source(stream_name)
        if not ds:
            return jsonify({'error': 'Not found'}), 404
        return jsonify({'data_source': ds})

    @app.route('/api/network/data-source', methods=['POST'])
    @login_required
    def api_network_data_source_create():
        """Create a new data source and its corresponding publication."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or not data.get('stream_name'):
            return jsonify({'error': 'Missing stream_name'}), 400
        url = data.get('url', '').strip()
        cadence = data.get('cadence_seconds') or 0
        parser_type = data.get('parser_type', '') if url else ''
        parser_config = data.get('parser_config', '') if url else ''
        # If URL is provided, parser config is required
        if url and not parser_config:
            return jsonify({'error': 'Parser config required when URL is set'}), 400
        # Create the data source
        startup.networkDB.add_data_source(
            stream_name=data['stream_name'],
            url=url,
            cadence_seconds=cadence,
            parser_type=parser_type,
            parser_config=parser_config,
            name=data.get('name', ''),
            description=data.get('description', ''),
            method=data.get('method', 'GET'),
            headers=data.get('headers'))
        # Create corresponding publication
        startup.networkDB.add_publication(
            stream_name=data['stream_name'],
            name=data.get('name', ''),
            description=data.get('description', ''),
            cadence_seconds=cadence or None)
        return jsonify({'success': True})

    @app.route('/api/network/publish', methods=['POST'])
    @login_required
    def api_network_publish():
        """Push a value to an existing publication.

        For externally-fed streams where the neuron doesn't fetch data itself.
        The caller provides the stream_name and value, and the neuron publishes
        it to all connected relays.
        """
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'stream_name' not in data or 'value' not in data:
            return jsonify({'error': 'Missing stream_name or value'}), 400
        stream_name = data['stream_name']
        value = data['value']
        # Verify publication exists
        pubs = startup.networkDB.get_active_publications()
        pub = next((p for p in pubs if p['stream_name'] == stream_name), None)
        if not pub:
            return jsonify({'error': f'No active publication: {stream_name}'}), 404
        try:
            startup.publishObservation(stream_name, value)
            return jsonify({'success': True, 'stream_name': stream_name})
        except Exception as e:
            logger.error(f"Publish error: {e}")
            return jsonify({'error': str(e)}), 500

    @app.route('/api/network/relays', methods=['GET'])
    @login_required
    def api_network_relays():
        """Return all known relays merged from central + local DB."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        # Get relays from local DB (keyed by URL)
        db_relays = {r['relay_url']: r for r in startup.networkDB.get_relays()}
        # Merge in relays from central
        try:
            server_relays = startup.server.getRelays()
            for r in server_relays:
                url = r['relay_url']
                if url not in db_relays:
                    db_relays[url] = {
                        'relay_url': url,
                        'first_seen': None,
                        'last_active': None,
                        'source': 'server',
                    }
                else:
                    db_relays[url]['source'] = 'both'
        except Exception:
            pass
        # Mark source for DB-only relays
        for url, r in db_relays.items():
            if 'source' not in r:
                r['source'] = 'local'
        relays = sorted(db_relays.values(),
                        key=lambda r: r.get('last_active') or 0, reverse=True)
        return jsonify({'relays': relays})

    @app.route('/api/network/relay', methods=['POST'])
    @login_required
    def api_network_relay_add():
        """Add a relay to the local DB."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'relay_url' not in data:
            return jsonify({'error': 'Missing relay_url'}), 400
        relay_url = data['relay_url'].strip()
        if not relay_url.startswith(('wss://', 'ws://')):
            return jsonify({'error': 'Relay URL must start with wss:// or ws://'}), 400
        startup.networkDB.upsert_relay(relay_url)
        return jsonify({'success': True})

    @app.route('/api/network/relay', methods=['DELETE'])
    @login_required
    def api_network_relay_delete():
        """Delete a relay from the local DB."""
        startup = get_startup()
        if not startup:
            return jsonify({'error': 'Startup not initialized'}), 503
        data = request.get_json()
        if not data or 'relay_url' not in data:
            return jsonify({'error': 'Missing relay_url'}), 400
        startup.networkDB.delete_relay(data['relay_url'])
        return jsonify({'success': True})

    @app.route('/api/wallet/download', methods=['GET'])
    @login_required
    def api_wallet_download():
        """Download wallet folder as ZIP compatible with wallet import."""
        import io
        import zipfile
        import datetime
        from pathlib import Path
        from satorineuron import config

        try:
            wallet_dir = Path(config.walletPath())
            if not wallet_dir.exists():
                return jsonify({'error': 'Wallet directory not found'}), 404

            allowed_files = ['wallet.yaml', 'vault.yaml', 'wallet.yaml.bak', 'vault.yaml.bak']
            required_files = ['wallet.yaml', 'vault.yaml']

            missing_required = [name for name in required_files if not (wallet_dir / name).exists()]
            if missing_required:
                return jsonify({
                    'error': f'Missing required wallet files: {", ".join(missing_required)}'
                }), 404

            archive = io.BytesIO()
            with zipfile.ZipFile(archive, mode='w', compression=zipfile.ZIP_DEFLATED) as zipf:
                for file_name in allowed_files:
                    src = wallet_dir / file_name
                    if src.exists() and src.is_file():
                        # Keep top-level "wallet" folder so extracted content is import-compatible.
                        zipf.write(src, arcname=f'wallet/{file_name}')

            archive.seek(0)
            timestamp = datetime.datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            return send_file(
                archive,
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'wallet_backup_{timestamp}.zip'
            )
        except Exception as e:
            logger.error(f"Wallet download error: {e}")
            return jsonify({'error': str(e)}), 500
