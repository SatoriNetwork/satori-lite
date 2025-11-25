# Satori Lite

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

## Configuration

The system focuses on:
1. Central server communication (checkin, authentication)
2. Wallet management (Evrmore)
3. Stream subscriptions and publications
4. Basic data flow

All unnecessary complexity has been removed to create a clean, maintainable codebase.
