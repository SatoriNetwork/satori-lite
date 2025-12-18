# Satori Lite

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A simplified, lightweight version of the Satori neuron system with a CLI interface (similar to Claude Code).

## Structure

```
satori-lite/
├── lib-lite/          # Minimal satorilib with only essential features
├── neuron-lite/       # Lightweight neuron implementation
├── engine-lite/       # AI engine (to be added)
└── requirements.txt   # Python dependencies
```

## Features

### lib-lite
Minimal version of satorilib containing only:
- **Central Server Communication**: Checkin, balances, stream management
- **Wallet Support**: Evrmore blockchain wallet and identity
- **Stream Data Structures**: StreamId, Stream, StreamPairs, StreamOverview
- **Utilities**: Disk operations, IP utilities, async threading
- **Optional Centrifugo**: Real-time messaging support (can be enabled/disabled)

### Excluded Features
To keep the system simple and lightweight, the following are NOT included:
- P2P networking
- Data relay engines
- IPFS integration
- Complex data management systems

## Installation

1. **Install lib-lite package:**
   ```bash
   cd lib-lite
   pip install -e .
   ```

2. **Install with optional features:**
   ```bash
   # With Centrifugo support
   pip install -e ".[centrifugo]"

   # With telemetry support
   pip install -e ".[telemetry]"

   # With all optional features
   pip install -e ".[centrifugo,telemetry]"
   ```

3. **Or install from requirements.txt:**
   ```bash
   pip install -r requirements.txt
   ```

## Usage

The neuron-lite will have a CLI interface similar to Claude Code for easy interaction and monitoring.

## Testing

Satori-lite has a comprehensive test suite with **155 tests** covering all functionality:

### Test Organization

```
tests/
├── unit/                  (27 tests - fast, mocked)
│   ├── test_auth_unit.py
│   └── test_client_unit.py
├── integration/           (85 tests - requires server)
│   ├── test_health.py
│   ├── test_auth.py
│   ├── test_predictions.py
│   ├── test_peer.py
│   ├── test_balance.py
│   ├── test_lending.py
│   ├── test_pool.py
│   ├── test_workflows.py
│   ├── test_data_persistence.py    # NEW: Phase 6
│   ├── test_challenge_lifecycle.py # NEW: Phase 6
│   └── test_user_journeys.py       # NEW: Phase 6
└── performance/           (37 tests - load & benchmarks)
    ├── test_performance.py
    ├── test_load.py
    └── test_edge_cases.py
```

### Running Tests

**Start the server first** (required for integration and performance tests):
```bash
export DATABASE_URL="sqlite:///:memory:"
uvicorn src.main:app --host 0.0.0.0 --port 8000 &
```

**Run all tests:**
```bash
cd /app/satori-lite
pytest tests/ -v
# 155 tests pass in ~23 seconds
```

**Run specific test categories:**
```bash
# Unit tests only (no server required)
pytest tests/unit/ -v                    # 27 tests in ~2 seconds

# Integration tests (requires server)
pytest tests/integration/ -v              # 85 tests

# Performance tests (requires server)
pytest tests/performance/ -v              # 37 tests

# By marker
pytest -m unit                           # All unit tests
pytest -m integration                    # All integration tests
pytest -m slow                           # Slow tests (performance)
```

**Run with coverage:**
```bash
pytest tests/ --cov=lib-lite/satorilib/server --cov-report=term-missing
```

### Test Coverage

- ✅ **100% API endpoint coverage** (all 15 endpoints)
- ✅ **Complete workflows** (authentication, predictions, observations)
- ✅ **Performance benchmarks** (response times, throughput)
- ✅ **Load testing** (concurrent operations, burst traffic)
- ✅ **Edge cases** (boundary conditions, data validation)

### Performance Benchmarks

All benchmarks validated with automated tests:

- Health endpoint: < 100ms average
- Challenge generation: < 150ms average
- Observation retrieval: < 200ms average
- Concurrent operations: > 95% success rate
- Throughput: > 20 requests/second

For detailed test documentation, see: [Test Plan Documentation](/app/docs/plans/)

## Configuration

The system focuses on:
1. Central server communication (checkin, authentication)
2. Wallet management (Evrmore)
3. Stream subscriptions and publications
4. Basic data flow

All unnecessary complexity has been removed to create a clean, maintainable codebase.
