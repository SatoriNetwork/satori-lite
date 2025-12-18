#!/bin/bash
#
# Satori-Lite Local Deployment Script
# ====================================
# Runs both the API server and satori-lite Web UI with REAL wallets.
# NO MOCKING - production-like environment.
#
# Usage:
#   ./deploy-local.sh          # Start everything
#   ./deploy-local.sh --stop   # Stop everything
#   ./deploy-local.sh --rebuild # Rebuild and start
#   ./deploy-local.sh --logs   # Show logs
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_banner() {
    echo -e "${BLUE}"
    echo "================================================"
    echo "  SATORI-LITE LOCAL DEPLOYMENT"
    echo "  NO MOCKING - PRODUCTION-LIKE ENVIRONMENT"
    echo "================================================"
    echo -e "${NC}"
}

print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Create data directory for wallet persistence
create_data_dir() {
    if [ ! -d "$SCRIPT_DIR/data" ]; then
        print_status "Creating data directory for wallet persistence..."
        mkdir -p "$SCRIPT_DIR/data"
    fi
}

# Ensure Docker network exists
ensure_network() {
    # Remove manually created network if it conflicts with compose
    if docker network inspect app_default >/dev/null 2>&1; then
        # Check if network was created by compose
        local label=$(docker network inspect app_default --format '{{index .Labels "com.docker.compose.network"}}')
        if [ -z "$label" ]; then
            print_warning "Removing conflicting network 'app_default'..."
            docker network rm app_default 2>/dev/null || true
        fi
    fi
}

# Check if API server is running
check_api() {
    print_status "Checking API server status..."
    if docker ps --format '{{.Names}}' | grep -q "^satori-api$"; then
        print_status "API server (satori-api) is running"
        return 0
    else
        print_warning "API server (satori-api) is NOT running"
        return 1
    fi
}

# Start API server from parent docker-compose
start_api() {
    print_status "Starting API server..."
    cd "$ROOT_DIR"

    if docker ps --format '{{.Names}}' | grep -q "^satori-api$"; then
        print_status "API server already running"
    else
        print_status "Starting database and API server..."
        docker compose up -d db api

        # Wait for API to be healthy
        print_status "Waiting for API server to be healthy..."
        for i in {1..30}; do
            if curl -s http://localhost:8000/health >/dev/null 2>&1; then
                print_status "API server is healthy!"
                break
            fi
            sleep 1
            if [ $i -eq 30 ]; then
                print_error "API server failed to become healthy"
                exit 1
            fi
        done
    fi
}

# Build satori-lite container
build_satori_lite() {
    print_status "Building satori-lite container..."
    cd "$SCRIPT_DIR"
    docker build -t satori-lite .
}

# Start satori-lite container
start_satori_lite() {
    print_status "Starting satori-lite container..."
    cd "$SCRIPT_DIR"

    # Stop existing container if running
    docker stop satori-lite 2>/dev/null || true
    docker rm satori-lite 2>/dev/null || true

    # Run with real wallet data persistence
    docker run -d \
        --name satori-lite \
        --network app_default \
        -p 24601:24601 \
        -e SATORI_UI_PORT=24601 \
        -v "$SCRIPT_DIR/data:/data" \
        -e SATORI_API_URL=http://satori-api:8000 \
        -e WALLET_PATH=/data/wallet.yaml \
        -e VAULT_PATH=/data/vault.yaml \
        -e SECRET_KEY=local-dev-secret-key \
        satori-lite

    # Wait for web UI to be healthy
    print_status "Waiting for satori-lite to be healthy..."
    for i in {1..20}; do
        if curl -s http://localhost:24601/health >/dev/null 2>&1; then
            print_status "satori-lite is healthy!"
            break
        fi
        sleep 1
        if [ $i -eq 20 ]; then
            print_warning "satori-lite may still be starting..."
        fi
    done
}

# Stop all services
stop_all() {
    print_status "Stopping satori-lite..."
    docker stop satori-lite 2>/dev/null || true
    docker rm satori-lite 2>/dev/null || true

    print_status "Stopping API server..."
    cd "$ROOT_DIR"
    docker compose down || true

    print_status "All services stopped"
}

# Show logs
show_logs() {
    echo ""
    echo -e "${BLUE}=== SATORI-LITE LOGS ===${NC}"
    docker logs satori-lite --tail 50 2>&1 || echo "satori-lite container not running"
    echo ""
    echo -e "${BLUE}=== SATORI-API LOGS ===${NC}"
    docker logs satori-api --tail 50 2>&1 || echo "satori-api container not running"
}

# Print final status
print_final_status() {
    echo ""
    echo -e "${GREEN}================================================${NC}"
    echo -e "${GREEN}  DEPLOYMENT COMPLETE!${NC}"
    echo -e "${GREEN}================================================${NC}"
    echo ""
    echo -e "  ${BLUE}Web UI:${NC}     http://localhost:24601"
    echo -e "  ${BLUE}API Server:${NC} http://localhost:8000"
    echo ""
    echo -e "  ${YELLOW}IMPORTANT:${NC}"
    echo -e "  - First login will CREATE a new wallet/vault"
    echo -e "  - Choose a password you will remember"
    echo -e "  - Wallet data is persisted in: $SCRIPT_DIR/data/"
    echo ""
    echo -e "  ${YELLOW}To stop:${NC}     ./deploy-local.sh --stop"
    echo -e "  ${YELLOW}To rebuild:${NC}  ./deploy-local.sh --rebuild"
    echo -e "  ${YELLOW}To see logs:${NC} ./deploy-local.sh --logs"
    echo ""
}

# Main logic
main() {
    print_banner

    case "${1:-}" in
        --stop)
            stop_all
            exit 0
            ;;
        --logs)
            show_logs
            exit 0
            ;;
        --rebuild)
            print_status "Rebuilding everything..."
            stop_all
            create_data_dir
            ensure_network
            start_api
            build_satori_lite
            start_satori_lite
            print_final_status
            ;;
        *)
            create_data_dir
            ensure_network
            start_api

            # Only build if image doesn't exist or --rebuild specified
            if ! docker image inspect satori-lite >/dev/null 2>&1; then
                build_satori_lite
            else
                print_status "Using existing satori-lite image (use --rebuild to force rebuild)"
            fi

            start_satori_lite
            print_final_status
            ;;
    esac
}

main "$@"
