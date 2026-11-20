"""
Satori-Lite Web UI Flask Application.

Minimal web interface for satori-lite functionality:
- Wallet unlock/login
- Dashboard with balance, rewards, lending, and pool management
"""
import os
import secrets
from datetime import timedelta
from flask import Flask


def create_app(testing=False):
    """
    Create and configure the Flask application.

    Args:
        testing: If True, configure for testing mode

    Returns:
        Flask application instance
    """
    app = Flask(__name__,
                template_folder='templates',
                static_folder='static')

    # Configuration
    # Sessions are signed with SECRET_KEY when it is set, else a random secret
    # of this start (sessions already end with a restart).
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    app.config['TESTING'] = testing

    # Session configuration - sessions expire after 24 hours
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

    # FIX: Set unique session cookie name per port to prevent cookie collision
    # between multiple instances running on localhost:24601, localhost:24602, etc.
    # Without this, browsers share the same session cookie across all ports,
    # causing JWT tokens and session data to be overwritten between instances.
    ui_port = os.environ.get('SATORI_UI_PORT', '24601')
    app.config['SESSION_COOKIE_NAME'] = f'satori_session_{ui_port}'

    # Enable template auto-reload in dev mode (no restart needed for HTML/CSS/JS)
    if os.environ.get('SATORI_ENV') == 'dev':
        app.config['TEMPLATES_AUTO_RELOAD'] = True
        app.jinja_env.auto_reload = True

    # Server API URL (for proxying requests)
    from satorilib.config import get_api_url
    app.config['SATORI_API_URL'] = get_api_url()

    # Register routes
    from web.routes import register_routes
    register_routes(app)

    # Register self-improvement endpoints (skill, API index, diff submission)
    from web.self_improve import register_self_improve_routes
    register_self_improve_routes(app)

    return app


# For running directly
if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True)
