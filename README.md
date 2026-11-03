# Satori Lite

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Satori Lite is a streamlined distribution of the Satori Network neuron, designed
to run a complete prediction node with a minimal footprint. It bundles a
forecasting engine, an Evrmore wallet, a stream relay, and a local web
interface into a single deployable unit suitable for individual operators,
research deployments, and integration testing.

The project is composed of two repositories that are intended to be used
together:

- [satori-lite](https://github.com/SatoriNetwork/satori-lite) — the neuron, the
  AI engine, the embedded relay tooling, and the operator-facing web and CLI
  interfaces.
- [satorilib](https://github.com/SatoriNetwork/satorilib) — the shared Python
  library providing wallets, server clients, datastream concepts, transport
  adapters, and the Nostr-based publish/subscribe stack used by the neuron.

## Overview

A Satori neuron consumes public datastreams, trains models against them, and
publishes its own predictions back to the network as new datastreams. Satori
Lite preserves this end-to-end behavior while removing infrastructure that is
not required for an individual node, producing a build that is easier to
operate, audit, and extend.

The neuron is organized as three cooperating components:

- **neuron-lite** — process supervisor, configuration, wallet management,
  scheduling, scoring, and the operator interfaces (CLI and web UI).
- **engine-lite** — the forecasting engine responsible for feature
  engineering, model training, evaluation, and prediction publication.
- **lib-lite / satorilib** — shared primitives for keys and signing, stream
  data structures, server communication, persistence, and transport.

## Repository Layout

```
satori-lite/
  neuron-lite/         Neuron runtime, CLI, scheduling, wallet, scoring
  engine-lite/         Forecasting engine, adapters, storage, model code
  lib-lite/            Vendored subset of satorilib used by the neuron
  web/                 Operator web interface (Flask application)
  docs/                Architecture, guides, and implementation notes
  migrations/          Schema migrations for local persistence
  tests/               Unit, integration, and performance test suites
  Dockerfile           Container build for the full neuron
  docker-compose.local.yml
  requirements.txt
```

The companion `satorilib` repository provides the wider library surface,
including additional transports, the Nostr datastream stack
(`satorilib.satori_nostr`), payment-channel primitives, and modules that are
not required by the lightweight neuron build but are available for advanced
deployments.

## Key Components

### Neuron (`neuron-lite`)

- Startup orchestration via a directed acyclic graph of initialization steps.
- The two keys: the alias (made at startup) and the wallet (made at wallet
  setup, unlocked with its password), and the wallet's link to the alias.
- Local stream relay management and publication scheduling.
- Scoring and reward tracking against the central server.
- A Flask-based web interface served on port `24601`.
- An interactive terminal CLI for wallet and node operations.

### Engine (`engine-lite`)

- Model training and continuous retraining for assigned datastreams.
- Pluggable adapters for data acquisition and storage backends.
- Prediction emission as first-class datastreams consumable by other neurons.

### Shared Library (`satorilib`)

- **Keys**: Evrmore key classes, key derivation, and signing primitives.
- **Server**: Authenticated client for the Satori central server (checkin,
  balances, stream and subscription management).
- **Concepts**: Canonical data structures for streams, observations, and
  related domain objects.
- **Transports**: Nostr-based datastream pub/sub
  (`satorilib.satori_nostr`), Centrifugo client, websockets, and additional
  experimental transports.
- **Persistence**: SQLite helpers, on-disk caching, and file utilities.
- **Asynchronous**: Thread and task helpers used across the neuron.

### The alias and the wallet

The neuron holds two keys:

- the **alias**: its automated signing key. It is made at startup with no
  input and kept unencrypted, so the neuron works with nobody present. It
  acts for the wallet with the Satori Network (register, login, predictions)
  and signs the neuron's Base predictions and payment-channel vouchers.
- the **wallet**: the person's own key, which holds the funds (the SATORI,
  the stake, the claims). It is made at wallet setup and encrypted with the
  person's password.

Key files, in the keys folder (`/Satori/Neuron/wallet` in the image): a new
install writes `alias.yaml` (the alias) and `wallet.yaml` (the wallet). An
install from before the rename keeps `wallet.yaml` (the alias) and
`vault.yaml` (the wallet), and the neuron never moves or renames them. The
two layouts are told apart only by `alias.yaml`: present, the new layout;
absent with `wallet.yaml` present, the older one (`config.keyFiles()`).
Download and import of the wallet zip carry both files of the layout in use,
named by that layout.

### The Satori Network: register, login and the link

When the wallet is unlocked (at wallet setup, and at login when no link is
stored yet), it signs a **link** to its alias: an EIP-191 (`personal_sign`)
signature by the wallet of exactly

```
Satori: link this alias to my wallet
alias: 0x<the alias's address, EIP-55>
wallet: 0x<the wallet's address, EIP-55>
issued: <unix seconds>
```

The link (the message and its signature; nothing secret) is kept in
`wallet-link.yaml` beside the keys and sent with every register.

- **Register** (`POST /api/v1/peer/register`) sends `alias-pubkey`,
  `nostr-pubkey`, `neuron-version` and, once a wallet exists, its link as
  `wallet-link` (base64 of the message's UTF-8) and `wallet-signature`.
  Before a wallet exists the alias registers alone. The answer's
  `wallet_address` is the linked wallet's Base address (or null), and
  `wallet_link_error` says why a link was not accepted.
- **Login** (`POST /api/v1/auth/login`, satorilib's `SatoriServerClient` and
  the web UI's `get_auth_headers`) and the legacy header auth (`authPayload`)
  send `alias-pubkey` (compressed secp256k1 hex), `message` and `signature`,
  where `signature` is EIP-191 by the alias: `0x` + r‖s‖v hex. Central
  recovers the signer and compares it with the Ethereum address of
  `alias-pubkey`.

The neuron image must be built with satorilib `base-upgrade` (with
`satorilib.wallet.link`), and it needs a central that accepts this form of
register and login: central's `base-upgrade` branch. The two deploy
together: central first, or at the same time, never the neuron first.

## Installation

### From source

Clone both repositories side by side and install them into the same Python
environment:

```bash
git clone https://github.com/SatoriNetwork/satorilib.git
git clone https://github.com/SatoriNetwork/satori-lite.git

pip install -e ./satorilib
pip install -r ./satori-lite/requirements.txt
```

Python 3.10 or newer is recommended. A C toolchain and the development
headers for `libsecp256k1`, `libleveldb`, `liblmdb`, and `libssl` are
required to build the cryptography and storage dependencies on Linux.

### Using Docker

A reference container build is provided. The build expects `satorilib` to be
supplied as an external build context so the library can be installed into the
image without publishing it as a package:

```bash
docker build \
  --build-context satorilib=https://github.com/SatoriNetwork/satorilib.git \
  -t satori-lite:local \
  https://github.com/SatoriNetwork/satori-lite.git
```

A Compose file (`docker-compose.local.yml`) is included for local development
and exposes the web interface on port `24601`.

## Running the Neuron

After installation, start the neuron from the `neuron-lite` directory:

```bash
python neuron-lite/start.py
```

The process performs wallet initialization, registers with the central
server, starts the embedded relay, launches the engine, and serves the
operator web interface at `http://localhost:24601`.

The interactive CLI can be used for wallet inspection and management, and
operational commands:

```bash
python neuron-lite/cli.py
```

## Configuration

Configuration is layered and resolved in the following order:

1. Built-in defaults shipped with the neuron.
2. YAML configuration files under `neuron-lite/config/`.
3. Environment variables.
4. Command-line flags supplied to `start.py`.

The minimum configuration required for a working node is the central server
endpoint and a wallet location. Additional settings control the engine,
relay, scoring policy, and optional transports. See `docs/guides/` for
operator-focused documentation and `docs/architecture/` for design
references.

## Testing

The repository ships with a layered test suite covering unit, integration,
and performance scenarios.

```bash
pytest tests/                  # full suite
pytest tests/unit/             # fast, isolated tests
pytest tests/integration/      # require a running local server
pytest tests/performance/      # load and benchmark tests
```

Markers are available for selective runs (`-m unit`, `-m integration`,
`-m slow`). Coverage can be measured against the bundled library subset:

```bash
pytest tests/ --cov=lib-lite/satorilib --cov-report=term-missing
```

Integration and performance tests expect the local API to be reachable on
`http://localhost:8000`. See `tests/README` and `docs/plans/` for detailed
instructions.

## Documentation

In-tree documentation is organized under `docs/`:

- `docs/architecture/` — system design and component interactions.
- `docs/engine/` — forecasting engine internals and training pipeline.
- `docs/guides/` — operator and developer guides.
- `docs/implementation/` — implementation notes for specific subsystems.
- `docs/plans/` — design and test plans.

## Contributing

Contributions are welcome. Before submitting a pull request, please:

1. Open an issue describing the proposed change.
2. Ensure the relevant test suites pass locally.
3. Follow the existing module layout and avoid introducing dependencies that
   would expand the lightweight footprint of the neuron.

For changes that affect shared primitives (wallets, server clients, stream
concepts, transports), please open the corresponding pull request against the
`satorilib` repository.

## License

Satori Lite is released under the MIT License. See `LICENSE` for the full
text.
