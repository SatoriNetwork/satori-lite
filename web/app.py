"""
Satori-Lite Web UI Flask Application.

Minimal web interface for satori-lite functionality:
- Vault unlock/login
- Dashboard with balance, rewards, lending, and pool management
"""
import os
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
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    app.config['TESTING'] = testing

    # Session configuration - sessions expire after 24 hours
    app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

    # Server API URL (for proxying requests)
    from satorilib.config import get_api_url
    app.config['SATORI_API_URL'] = get_api_url()

    # Register routes
    from web.routes import register_routes
    register_routes(app)

    return app


# For running directly
if __name__ == '__main__':
    app = create_app()
    app.run(host='0.0.0.0', port=5000, debug=True)
