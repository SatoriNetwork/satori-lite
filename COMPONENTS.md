# Satori Lite Components

## Overview

This document describes what was copied from the main Lib repository to create the lightweight lib-lite package.

## Directory Structure

```
satori-lite/
├── lib-lite/
│   └── satorilib/
│       ├── __init__.py          # Main package init with key exports
│       ├── concepts/            # Stream data structures
│       │   ├── structs.py       # StreamId, Stream, StreamPairs, StreamOverview
│       │   ├── constants.py     # Constants used across the system
│       │   └── __init__.py
│       ├── wallet/              # Blockchain wallet support
│       │   ├── wallet.py        # Base wallet implementation
│       │   ├── identity.py      # Identity management
│       │   ├── evrmore/         # Evrmore blockchain specifics
│       │   ├── ethereum/        # Ethereum wallet support
│       │   └── utils/           # Wallet utilities
│       ├── server/              # Central server communication
│       │   ├── server.py        # SatoriServerClient
│       │   ├── api.py           # CheckinDetails, API definitions
│       │   └── __init__.py
│       ├── utils/               # General utilities
│       │   ├── ip.py            # getPublicIpv4UsingCurl
│       │   ├── hash.py          # Hashing functions
│       │   ├── time.py          # Time utilities
│       │   ├── secret.py        # Encryption/decryption
│       │   └── ...              # Other utilities
│       ├── asynchronous/        # Async support
│       │   ├── thread.py        # AsyncThread
│       │   └── __init__.py
│       ├── disk/                # File operations
│       │   └── ...              # Disk I/O utilities
│       ├── electrumx/           # Blockchain node communication
│       │   └── ...              # ElectrumX client
│       ├── logging/             # Logging utilities
│       │   └── ...              # Custom logging setup
│       └── centrifugo/          # OPTIONAL: Real-time messaging
│           └── ...              # Centrifugo client
├── neuron-lite/
│   └── start.py                 # Copied from Neuron/satorineuron/init/start.py
├── requirements.txt             # Python dependencies
├── README.md                    # Project overview
├── INSTALL.md                   # Installation instructions
└── test_imports.py              # Import verification script
```

## Key Modules

### 1. concepts/ (REQUIRED)
**Purpose**: Core data structures for streams and observations

**Files copied**:
- `structs.py` - StreamId, Stream, StreamPairs, StreamOverview, Observation
- `constants.py` - System constants (stake requirements, etc.)

**Dependencies**: pandas, typing

**Used by**: Start.py lines 11-15

### 2. wallet/ (REQUIRED)
**Purpose**: Blockchain wallet management for Evrmore and Ethereum

**Files copied**: Entire wallet directory including:
- `wallet.py` - Base EvrmoreWallet class
- `identity.py` - Identity management
- `evrmore/` - Evrmore blockchain implementation
- `ethereum/` - Ethereum wallet support
- `utils/` - Wallet utilities

**Dependencies**: python-evrmorelib, eth-account, cryptography

**Used by**: Start.py lines 18-19

### 3. server/ (REQUIRED)
**Purpose**: Communication with Satori central server

**Files copied**:
- `server.py` - SatoriServerClient for API calls
- `api.py` - CheckinDetails, API response structures

**Dependencies**: requests, marshmallow

**Used by**: Start.py lines 20-21

### 4. utils/ (REQUIRED)
**Purpose**: General utility functions

**Files copied**: Entire utils directory including:
- `ip.py` - Get public IPv4 address
- `hash.py` - Hashing utilities
- `time.py` - Time conversions
- `secret.py` - Encryption/decryption
- `dict.py` - Dictionary utilities
- And more...

**Used by**: Start.py line 37

### 5. asynchronous/ (REQUIRED)
**Purpose**: Async task management

**Files copied**:
- `thread.py` - AsyncThread class for running async tasks
- `generator.py` - Async generators

**Dependencies**: asyncio

**Used by**: Start.py line 23

### 6. disk/ (REQUIRED)
**Purpose**: File and disk operations

**Files copied**: Entire disk directory

**Dependencies**: PyYAML, os, pathlib

**Used by**: Start.py line 16

### 7. electrumx/ (REQUIRED - dependency of wallet)
**Purpose**: Communication with Evrmore blockchain nodes

**Files copied**: Entire electrumx directory

**Dependencies**: websockets, asyncio

**Used by**: Wallet module

### 8. logging/ (REQUIRED)
**Purpose**: Logging utilities

**Files copied**: Entire logging directory

**Used by**: Throughout the codebase

### 9. centrifugo/ (OPTIONAL)
**Purpose**: Real-time messaging via Centrifugo

**Files copied**: Entire centrifugo directory

**Dependencies**: centrifuge-python (optional)

**Used by**: Start.py line 22 (optional, can be disabled)

## Excluded Components

The following were intentionally excluded to keep lib-lite minimal:

### NOT Copied:
- **datamanager/** - Complex data server/client system (not needed for lite version)
- **pubsub/** - Legacy pubsub system (replaced by centrifugo, which is optional)
- **ftp/** - FTP server functionality
- **ipfs/** - IPFS integration
- **gossip/** - Gossip protocol
- **thunder/** - Thunder protocol
- **zeromq/** - ZeroMQ messaging
- **validate/** - Advanced validation
- **telemetry/** - Telemetry logging (available as optional extra)
- **sqlite/** - SQLite integration (can be added if needed)

## Dependencies Summary

### Core Dependencies (Required):
```
pandas==1.5.2
numpy==1.24.0
PyYAML==6.0
python-evrmorelib
eth-account==0.13.7
web3==7.12.1
requests>=2.31.0
websockets==15.0.1
cryptography==44.0.2
pycryptodome==3.20.0
marshmallow==3.22.0
```

### Optional Dependencies:
```
centrifuge-python==0.4.1  # For Centrifugo support
sanic>=23.6.0             # For telemetry
aiosqlite>=0.19.0         # For telemetry
```

## Size Comparison

- **Original Lib**: ~200+ Python files, many features
- **lib-lite**: ~85 Python files, focused on essentials

This represents a significant reduction while maintaining all functionality needed by start.py.
