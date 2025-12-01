"""
Satori-Lite Web UI Routes.

Handles all web routes for the minimal UI:
- Login/vault unlock
- Dashboard
- API proxy endpoints
"""
from functools import wraps
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

# Global vault reference (will be set by the application)
_vault = None


def set_vault(vault):
    """Set the vault instance for the routes to use."""
    global _vault
    _vault = vault


def get_vault():
    """Get the vault instance."""
    return _vault


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
        """Redirect to dashboard if logged in, otherwise to login."""
        if session.get('vault_open'):
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        """Handle vault unlock/login."""
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
        """Health check endpoint."""
        return jsonify({'status': 'ok'})

    # API Proxy Routes
    def proxy_api(endpoint, method='GET', data=None):
        """Proxy requests to the Satori API server."""
        api_url = current_app.config.get('SATORI_API_URL', 'http://satori-api:8000')
        url = f"{api_url}/api/v1{endpoint}"

        try:
            if method == 'GET':
                resp = requests.get(url, timeout=10)
            elif method == 'POST':
                resp = requests.post(url, json=data, timeout=10)
            elif method == 'DELETE':
                resp = requests.delete(url, json=data, timeout=10)
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
        return proxy_api('/pool/toggle-open', 'POST')

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
