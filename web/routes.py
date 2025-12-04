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

# Global vault reference (will be set by the application)
_vault = None


def check_vault_password_exists():
    """Check if vault password exists in config.

    Returns:
        bool: True if vault password exists and is not empty, False otherwise
    """
    try:
        from satorineuron import config
        vault_password = config.get().get('vault password')
        return vault_password is not None and len(str(vault_password)) > 0
    except Exception as e:
        logger.warning(f"Failed to check vault password: {e}")
        return False


def set_vault(vault):
    """Set the vault instance for the routes to use."""
    global _vault
    _vault = vault


def get_vault():
    """Get the vault instance."""
    return _vault


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
        if not session.get('vault_open'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


def register_routes(app):
    """Register all routes with the Flask app."""

    @app.route('/')
    def index():
        """Redirect to dashboard if logged in, vault setup if needed, otherwise to login."""
        # Check if vault password exists first
        if not check_vault_password_exists():
            return redirect(url_for('vault_setup'))

        if session.get('vault_open'):
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Handle vault unlock/login."""
        # Check if vault password exists, redirect to setup if not
        if not check_vault_password_exists():
            return redirect(url_for('vault_setup'))

        if request.method == 'POST':
            password = request.form.get('password', '')

            wallet_manager = get_vault()
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
                        # Successfully decrypted
                        session['vault_open'] = True

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
        # If vault password already exists, redirect to login with message
        if check_vault_password_exists():
            flash('Vault password already exists. Please log in with your existing password.', 'info')
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

            # Save password to config with double-check to prevent race condition
            try:
                from satorineuron import config

                # Double-check: verify password still doesn't exist before writing
                # This prevents race condition where CLI/another instance creates it
                # between our initial check and now
                if check_vault_password_exists():
                    flash('Vault password was already created. Please log in.', 'info')
                    logger.info("Vault password already exists (created elsewhere), redirecting to login")
                    return redirect(url_for('login'))

                config.add(data={'vault password': password})
                flash('Vault password created successfully! Please log in.', 'success')
                logger.info("Vault password created via web UI")
                return redirect(url_for('login'))
            except Exception as e:
                logger.error(f"Failed to save vault password: {e}")
                flash('Error saving vault password. Please try again.', 'error')
                return render_template('vault_setup.html')

        return render_template('vault_setup.html')

    @app.route('/logout')
    def logout():
        """Handle logout/vault lock."""
        wallet_manager = get_vault()
        if wallet_manager:
            try:
                wallet_manager.closeVault()
            except Exception:
                pass

        session.clear()
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
        wallet_manager = get_vault()
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
        return proxy_api('/lender/lend', request.method, request.json)

    @app.route('/api/pool/worker', methods=['POST', 'DELETE'])
    @login_required
    def api_pool_worker():
        """Proxy pool worker request."""
        return proxy_api('/pool/worker', request.method, request.json)

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

    @app.route('/api/wallet/address')
    @login_required
    def api_wallet_address():
        """Get wallet and vault addresses."""
        wallet_manager = get_vault()
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
        wallet_manager = get_vault()
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
        wallet_manager = get_vault()
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
        wallet_manager = get_vault()
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
                # Send all tokens
                txid = vault.sendAllTransaction(address)
            else:
                # Send specific amount
                txid = vault.typicalNeuronTransaction(
                    amount=amount,
                    address=address)

            if txid and len(txid) == 64:
                return jsonify({'success': True, 'txid': txid})
            else:
                return jsonify({'error': 'Transaction failed', 'details': str(txid)}), 500
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
        wallet_manager = get_vault()
        if not wallet_manager:
            return jsonify({'error': 'Wallet manager not initialized'}), 500

        try:
            # Ensure electrumx connection
            if hasattr(wallet_manager, 'connect'):
                connected = wallet_manager.connect()
                if not connected:
                    return jsonify({'error': 'Could not connect to electrumx'}), 500

            total_satori = 0.0
            wallet_balance = 0.0
            vault_balance = 0.0

            # Get wallet (identity) balance
            if wallet_manager.wallet:
                wallet = wallet_manager.wallet
                if hasattr(wallet, 'getBalances'):
                    wallet.getBalances()
                    if hasattr(wallet, 'balance') and wallet.balance:
                        wallet_balance = wallet.balance.amount if hasattr(wallet.balance, 'amount') else 0.0

            # Get vault balance
            if wallet_manager.vault:
                vault = wallet_manager.vault
                if hasattr(vault, 'getBalances'):
                    vault.getBalances()
                    if hasattr(vault, 'balance') and vault.balance:
                        vault_balance = vault.balance.amount if hasattr(vault.balance, 'amount') else 0.0

            total_satori = wallet_balance + vault_balance

            return jsonify({
                'total': total_satori,
                'wallet_balance': wallet_balance,
                'vault_balance': vault_balance
            })
        except Exception as e:
            logger.warning(f"Failed to get direct balance: {e}")
            return jsonify({'error': str(e)}), 500
