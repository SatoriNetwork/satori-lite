# Stream Market End-to-End Testing Environment

Status: **PLAN** — not yet implemented.
Scope: **stream market only** (layer 1 of the marketplace). The prediction
competition market (layer 2) will reuse this environment in a follow-up plan.

## 1. Goal

Stand up two real, persistent Satori neuron instances inside Docker and drive
them through a complete stream-market lifecycle with browser automation:

1. **Alice** (producer) creates a priced data-stream publication.
2. **Bob** (subscriber) discovers the stream via a shared Nostr relay.
3. Bob opens a payment channel to Alice on-chain (real EVR, real Satori).
4. Bob sends one or more channel payments.
5. Alice grants access and Bob begins receiving observations from the gated
   stream (events Bob was not allowed to see before the payment should be
   absent; events after should be present).
6. Tear-down preserves wallet + vault state so the next run starts from the
   same funded identity instead of a fresh keypair.

The environment is expected to be long-lived — the same two neuron instances
will be reused across test runs and across sessions. Tests should be able to
reset the networkDB between runs **without** regenerating wallets.

## 2. What we are running inside

This project lives inside a **docker-in-docker** dev container. The layout
looks like this:

```
host (jordan@laptop)
└── docker-daemon (image: docker:dind)       ← "inner" Docker daemon
    ├── repos0 (THIS container — where Claude runs)
    │     DOCKER_HOST=tcp://docker-daemon:2376
    │     mounts: ~/shared → /shared, ~/repos → /code
    └── ... other dev containers
```

Key consequences:

- `docker ...` commands run from this container **target the inner daemon**.
  Any containers we launch will run *alongside* us, **not inside us**.
- The inner daemon has its own isolated filesystem. The **only** path both
  `repos0` *and* the inner daemon can see is `/shared` (host `~/shared`,
  mounted into both). Therefore wallet volumes **must** live under
  `/shared/...` or they will not actually mount into the neuron containers.
- The inner daemon's port forwards are already punched through on the outer
  host: `14601 → 24601`, `13000-13010 → 3000-3010`, `15000 → 5000`,
  `18000 → 8000`. We will map neuron web UIs to ports in that range so they
  are reachable from outside the DinD if needed. Inside the dev container we
  can reach containers by their Docker DNS name on a user-defined network.

## 3. Architecture

```
                        ┌──────────────────────────────┐
                        │   inner docker network       │
                        │   satori-e2e-net (bridge)    │
                        │                              │
  ┌─────────────┐       │   ┌─────────────────────┐    │
  │ satori-e2e- │◀──────┼──▶│ satori-e2e-alice    │    │
  │    relay    │       │   │ neuron:stream-mkt   │    │
  │ (strfry)    │       │   │ 24601 → 14601       │    │
  │  7777       │       │   │ wallet: /data       │    │
  └─────────────┘       │   └─────────────────────┘    │
        ▲               │             ▲                │
        │               │             │                │
        │               │   ┌─────────────────────┐    │
        └───────────────┼──▶│ satori-e2e-bob      │    │
                        │   │ neuron:stream-mkt   │    │
                        │   │ 24601 → 14602       │    │
                        │   │ wallet: /data       │    │
                        │   └─────────────────────┘    │
                        └──────────────────────────────┘
```

### 3.1 Containers

| Container | Image | Purpose |
|---|---|---|
| `satori-e2e-relay` | existing Satori strfry relay (see `neuron/relay/`) or `scsibug/nostr-rs-relay` | Single Nostr relay shared by both neurons so discovery, subscription, channel-open, and payment DMs can travel between them. |
| `satori-e2e-alice` | `satori-neuron:e2e` (built from `neuron/Dockerfile`) | Producer. Creates priced publications, publishes observations. |
| `satori-e2e-bob` | `satori-neuron:e2e` | Subscriber. Discovers, opens a channel, pays, consumes observations. |

Both neurons run the unmodified production entrypoint
`python /Satori/Neuron/start.py`. We do **not** add test-only branches in
application code — all test-specific wiring lives in compose, env vars, and
the bring-up script.

### 3.2 Network

A dedicated user-defined bridge network `satori-e2e-net` is created on the
inner daemon. All three containers join it. They address each other by
container name:

- Neurons connect to the relay as `ws://satori-e2e-relay:7777`.
- The Playwright test driver connects to each neuron web UI by the inner
  daemon's published port (`http://docker-daemon:14601` and
  `http://docker-daemon:14602` from inside `repos0`; or
  `http://localhost:14601/14602` from the outer host).

### 3.3 Central API — minimal

Neuron startup wants to contact `SATORI_CENTRAL_URL` for peer registration
(`ensure_peer_registered`). The stream market itself does **not** need
central; but startup is noisy if central is unreachable.

Two options, in preference order:

1. **Point at the real central** (`https://network.satorinet.io`). This is
   what a production neuron does today and it does not spend funds. The
   only cost is one HTTPS call per startup. **Default.**
2. **Stub it out** with a tiny mock HTTP server on `satori-e2e-net` that
   answers `/api/v1/peer/login` and `/api/v1/peer/register` with success.
   Only fall back to this if the real central turns out to block test
   identities.

### 3.4 Electrumx / on-chain

Channel open/pay/claim run real Evrmore transactions. The neuron image
defaults to mainnet electrumx servers listed in
`satorineuron/config/__init__.py:87`. For the E2E environment we keep these
defaults — no testnet, no mock — because:

- The user has explicitly said "you're going to need actual Satori tokens"
  and wants persistent wallets funded with real value.
- The payment-channel logic does a multisig P2SH with a CSV timeout; a
  mocked electrumx would hide the most interesting failure modes.

**Block time is ~60s on Evrmore.** This means a fresh run where Bob opens
his first channel has to wait at least one confirmation before Alice sees
the channel funded. The Playwright test must poll `GET /api/channels` on
Alice rather than asserting synchronously.

## 4. On-disk layout (persistent state)

### 4.1 `/shared` is host-persistent

Verified by `mount` inside this dev container:

```
/home/jordan/.Private on /shared  type ecryptfs  ...
/home/jordan/.Private on /code    type ecryptfs  ...
```

Both `/shared` and `/code` resolve to the same physical storage on the
host (`/home/jordan/.Private`, ecryptfs-encrypted at rest). Files under
`/shared` persist across container restarts, DinD restarts, and host
reboots — the inner `docker-daemon` container mounts the same host
`~/shared` as `/shared` (see `/code/devs/docker-compose.yml`), which
is exactly why it is the one path both containers can see. Because it
is already real host storage, **we do not maintain a separate
automated backup tree** — a one-shot manual tarball after first-run
funding is mentioned in §4.4 for operators who want it.

All persistent state therefore lives under a single rooted path:

```
/shared/satori-e2e/
├── alice/
│   └── data/                     ← bind-mounted into alice's /data
│       ├── wallet.yaml           (generated on first run, never deleted)
│       ├── wallet.yaml.bak
│       ├── vault.yaml            (encrypted, password in .env)
│       ├── vault.yaml.bak
│       └── network.db            (can be wiped between runs)
├── bob/
│   └── data/
│       ├── wallet.yaml
│       ├── vault.yaml
│       └── network.db
├── relay/
│   └── data/                     ← strfry database
├── .env                          (shared secrets — see §4.2)
└── README.md                     (brief human-readable "how to use this")
```

The **bring-up script is idempotent**: if `alice/data/wallet.yaml`
already exists, it is never rewritten. First-run wallet generation
happens via the normal neuron login flow, after which the generated
files sit on disk and are reused forever.

### 4.2 `.env` file

```
ALICE_VAULT_PASSWORD=<chosen-at-first-run>
BOB_VAULT_PASSWORD=<chosen-at-first-run>
SATORI_CENTRAL_URL=https://network.satorinet.io
SATORI_ENV=prod
```

Not in git. The first run of the bring-up script generates the
passwords if they do not already exist. The test harness reads them to
drive the Playwright login flow.

### 4.3 Where the wallet actually lives inside the image

Traced through the code:

- `satorineuron/init/wallet.py:35` calls `config.walletPath('wallet.yaml')`.
- `satorineuron/config/__init__.py:75` delegates to `path(of='wallet')`,
  which reads config key `'absolute wallet path'` and falls back to
  `root('./wallet')`.
- `config.yaml.template` does **not** set `absolute wallet path`, so
  the fallback is authoritative.
- `root` is `partial(root, os.path.abspath(__file__), '../')` where
  `__file__` is `satorineuron/config/__init__.py`. The underlying
  `root()` in `config/config.py:83` does
  `abspath(join(dirname(dirname(path)), *args))`.

Plugging in the image path `/Satori/Neuron/satorineuron/config/__init__.py`:

```
dirname(dirname(...))        → /Satori/Neuron/satorineuron
join('../', './wallet')      → /Satori/Neuron/satorineuron/../wallet
abspath                      → /Satori/Neuron/wallet
```

So **wallet files land at `/Satori/Neuron/wallet/{wallet,vault,nostr}.yaml`**
inside the built image. `start.py:71,115,…` uses the same `walletPath()`
helper, so `nostr.yaml` and the `.bak` copies all live in the same
directory — one bind mount catches everything.

**Note on the existing compose file.** `docker-compose.local.yml` sets
`WALLET_PATH=/data/wallet.yaml` / `VAULT_PATH=/data/vault.yaml` and
mounts `./data:/data`, but:

1. Those two env vars are **never read anywhere** in the neuron source.
2. The Dockerfile symlinks `/Satori/Neuron/data → /Satori/Engine/db`,
   so `/data` inside the container is the **engine database**, not the
   wallet.

Result: the existing compose file's "wallet persistence" is broken for
rebuilt containers — the wallet ends up in the image layer at
`/Satori/Neuron/wallet/` and is lost the next time the container is
re-created. This is not our bug to fix in this plan, but the E2E
bring-up script must use the correct path:

```
-v /shared/satori-e2e/alice/data:/Satori/Neuron/wallet
-v /shared/satori-e2e/bob/data:/Satori/Neuron/wallet
```

The host-side name `data/` is kept purely as a label — it holds
`wallet.yaml`, `vault.yaml`, `nostr.yaml`, their `.bak` copies, and
nothing else. `network.db` and engine state live elsewhere inside the
container and are deliberately not persisted (reset between runs is a
feature).

### 4.4 Optional one-shot manual backup

`/shared` is already host storage, so day-to-day operation does not
need a backup routine. If an operator wants belt-and-suspenders
protection after the one-time funding step in Scenario A (§6.1), they
can tar the state tree once and stash the tarball anywhere they like:

```
tar -czf ~/satori-e2e-$(date -u +%Y-%m-%dT%H-%M-%SZ).tgz \
    -C /shared satori-e2e
```

No automation, no restore script, no `.gitignored` second tree. The
bring-up and tear-down flows do not touch backups at all.

## 5. Bring-up script

Path: `scripts/e2e/stream-market-up.sh`

Responsibilities, in order:

1. **Guard**: ensure we are running inside the dev container
   (`[ -n "$DOCKER_HOST" ]` and `/shared` is writable). Abort if a
   previous `satori-e2e-*` container is still running from an earlier
   session so we never end up with two neurons pointing at the same
   wallet file simultaneously.
2. **Create state directories** under `/shared/satori-e2e/` if missing.
   Never delete or overwrite existing wallet files.
3. **Load `.env`** from `/shared/satori-e2e/.env`, generating any
   missing passwords with `openssl rand` on first run and writing them
   back.
4. **Build the neuron image** against the inner daemon:
   ```
   docker -H tcp://docker-daemon:2376 \
     build \
     --build-context satorilib=/code/Satori/satorilib \
     -t satori-neuron:e2e \
     /code/Satori/neuron
   ```
5. **Create network** `satori-e2e-net` (ignore "already exists").
6. **Start the relay** container.
7. **Start alice** with:
   - `--network satori-e2e-net`
   - `-v /shared/satori-e2e/alice/data:/Satori/Neuron/wallet` (see §4.3
     for why this path, not `/data`)
   - `-p 14601:24601`
   - env: `SATORI_CENTRAL_URL`, `SATORI_ENV`, default relay URL
     `ws://satori-e2e-relay:7777`
8. **Start bob** — identical but `-p 14602:24601` and
   `-v /shared/satori-e2e/bob/data:/Satori/Neuron/wallet`.
9. **Wait for health**: poll
   `http://docker-daemon:14601/health` and `:14602/health` from inside
   this container until 200 or timeout.
10. **Print final status**: container names, URLs, wallet paths.

Tear-down: `scripts/e2e/stream-market-down.sh` stops the three
containers but never removes the `/shared/satori-e2e/*/data` directories.

Reset (optional): `scripts/e2e/stream-market-reset.sh` removes `network.db`
from both neurons but leaves `wallet.yaml` and `vault.yaml` untouched, so
the next run starts with a clean subscriptions/publications table but the
same funded identities.

## 6. Test scenarios

All scenarios below are **driven end-to-end through the browser**. We do
not bypass the UI by calling Flask routes directly — the point of E2E is
to prove the UI, the Flask layer, the neuron runtime, the local relay
network, and the wallet all agree with each other.

### 6.1 Scenario A — first-run funding (manual, one-time)

Not automated. Executed by a human once per machine:

1. `stream-market-up.sh` brings up empty-wallet containers.
2. Operator opens `http://localhost:14601`, creates a vault password,
   writes down the 12 words, funds the wallet with EVR (fees) and Satori
   (channel capital) from an external source.
3. Same for `http://localhost:14602`.
4. Operator commits the generated public keys (not seeds) into
   `tests/e2e/stream_market/identities.yaml` so automation knows who is
   who without having to unlock vaults itself.

### 6.2 Scenario B — discovery (free stream)

Smoke test. No payment.

- Alice creates publication `e2e-free-heartbeat` with `price_per_obs=0`.
- Alice publishes observation `"1"`.
- Bob visits the streams page, finds `e2e-free-heartbeat`, subscribes.
- Bob's observations tab shows the `"1"` observation within 10s.
- **Assert**: Bob's networkDB has exactly one observation, equal to `"1"`.

### 6.3 Scenario C — priced stream, no payment, access denied

- Alice creates publication `e2e-priced-ticker` with `price_per_obs=100`.
- Alice publishes observations `"A"`, `"B"`, `"C"` one per second.
- Bob subscribes but does **not** open a channel.
- **Assert**: Bob's observations tab for `e2e-priced-ticker` is empty
  after 15s (access gated).

### 6.4 Scenario D — priced stream, channel + payment, access granted

Continues from Scenario C's state.

1. Bob visits `/channels`, clicks "Open Channel" with Alice's wallet
   pubkey, `amount_sats=10000`, `minutes=60`.
2. Bob waits (poll `/api/channels`) until the channel appears on Alice's
   receiver list — this is the "funding TX confirmed + KIND_CHANNEL_OPEN
   received" rendezvous and can take a couple of blocks.
3. Bob clicks "Pay" on that channel for `pay_amount_sats=100`.
4. Alice publishes observation `"D"`.
5. **Assert**: Bob's observations tab contains `"D"` (and only `"D"`).
   Pre-payment observations A/B/C must remain absent — the fix in
   `6802876 Channel: grant access for this payment only, not cumulative total`
   is explicitly what this assertion protects.

### 6.5 Scenario E — channel exhaustion

- After Scenario D, Alice publishes a second paid observation `"E"` that
  costs more than the remaining channel balance.
- **Assert**: Bob does not receive `"E"` until he sends another channel
  payment covering it.

### 6.6 Scenario F — reclaim after timeout (optional, slow)

- Bob opens a channel with `minutes=1`.
- Does nothing, waits for the CSV timeout + 1 confirmation.
- Calls reclaim.
- **Assert**: wallet balance increases by approximately `amount_sats`
  minus fees.

This scenario is **gated behind an opt-in flag** because it takes minutes
of wall time and consumes fees on every run.

## 7. Playwright driver

Location: `tests/e2e/stream_market/test_stream_market.py`
Framework: `pytest` + `pytest-playwright` (sync API).

### 7.1 Fixtures

- `session`-scope `env_up` fixture runs `stream-market-up.sh` once and
  `stream-market-down.sh` on teardown. Skipped automatically if containers
  are already running — developer iteration stays fast.
- `session`-scope `alice_page` / `bob_page` fixtures launch two
  independent browser contexts (separate cookie jars) and log in using
  passwords from `/shared/satori-e2e/.env`.
- `function`-scope `reset_network_db` fixture optionally runs
  `stream-market-reset.sh` between tests that need a clean slate.

### 7.2 Page-object sketch

```python
class NeuronUI:
    def __init__(self, page: Page, base_url: str, password: str):
        self.page = page
        self.base_url = base_url
        self.password = password

    def login(self): ...
    def create_priced_publication(self, name: str, price_sats: int): ...
    def publish_observation(self, name: str, value: str): ...
    def go_discover(self): ...
    def subscribe(self, stream_name: str, provider_pubkey: str): ...
    def open_channel(self, receiver_pubkey: str, amount_sats: int, minutes: int): ...
    def pay_channel(self, p2sh_address: str, pay_amount_sats: int): ...
    def get_observations(self, stream_name: str, provider_pubkey: str) -> list[dict]: ...
    def wait_for_channel_funded(self, p2sh_address: str, timeout_s: int = 180): ...
```

Methods hit the same URLs a human would click: `/dashboard`,
`/channels`, etc. Where a page has no convenient selector we will add a
minimal `data-testid` attribute to the existing template — this is the
only production code change the E2E work is allowed to make, and each
added testid must be justified in the PR.

### 7.3 Running from Claude

Two modes, both supported:

- **Interactive / debugging**: Claude drives the browser directly through
  the `mcp__playwright__browser_*` tools pointed at
  `http://localhost:14601` / `:14602`. Good for exploratory checks and
  when a scenario is breaking in a surprising way.
- **Batch / regression**: `pytest tests/e2e/stream_market/ -k stream_market_flow`
  runs all scenarios headlessly. This is what CI (eventually) will run.

## 8. Things we are deliberately not doing (yet)

- **Resetting wallets.** A corrupt or drained wallet is a human-operator
  problem, not a test-script one.
- **Creating the central API locally.** We call the production central.
- **Prediction market testing.** Layer 2 gets its own plan that reuses
  this environment.
- **Parallel test runs.** Two neurons, one pair of ports, one shared
  state dir. Tests run serially.
- **CI integration.** The environment needs a stable funded wallet before
  it makes sense to run on every PR. First we prove it works locally.

## 9. Open items to resolve before implementation

Numbered so the implementation PR can close each one explicitly.

1. **Decide on the shared Nostr relay container.** Prefer reusing
   `neuron/relay/` if there is a strfry image there; otherwise pick a
   small external relay image and document why.
2. **Decide SATORI_CENTRAL_URL strategy** — real central (default) or
   mock. (§3.3)
3. **Confirm the login flow that Playwright needs to automate.**
   Specifically: does the dashboard require vault-unlock on every page
   load, or is session-based? Read `web/routes.py` login handlers and
   the `login.html` template to find the answer before writing the
   fixture.
4. **Discover UI selectors and add any missing `data-testid` attributes**
   required to reliably drive the publications, subscriptions, and
   channels pages.
5. **Pick a real funding amount** — enough for ~20 channel opens plus
   fees, but low enough to be comfortable losing to bugs.
6. **Figure out how Alice's wallet pubkey and nostr pubkey surface in
   the UI so Bob can paste them into the "Open Channel" form.** If
   there's no existing UI for this, add a small "my identity" panel.

**Closed during planning:**

- ~~Verify where `wallet.yaml` lives inside the image.~~ Resolved in
  §4.3: `/Satori/Neuron/wallet/`. Bind mount updated in §5 step 7.

## 10. File manifest (to be created)

```
scripts/e2e/
    stream-market-up.sh
    stream-market-down.sh
    stream-market-reset.sh
tests/e2e/stream_market/
    __init__.py
    conftest.py                   ← fixtures, env_up, browser contexts
    identities.yaml               ← post-funding pubkeys (committed)
    pages.py                      ← NeuronUI page-object
    test_stream_market.py         ← Scenarios B–E, F behind --slow
docs/plans/stream-market-e2e-testing.md   (this file)
```

No application code in `neuron-lite/`, `web/`, or `satorilib/` is expected
to change, with the single exception of `data-testid` selector additions
called out in open item #5.
