# Scalable Nostr Relay Connection Architecture

Why relay connections are unstable today, why relays sometimes get "connected
extra" (duplicate sockets), and a phased architecture for scaling as the
number of neurons and subscriptions grows. Companion to
[`FIREHOSE.md`](./FIREHOSE.md), which multiplies per-neuron relay connections
and therefore depends on the stability work here.

> Status: design only, not yet implemented. All file/line references verified
> against current code. Paths: neuron `neuron-lite/start.py`, client library
> `satorilib/src/satorilib/satori_nostr/`, relay discovery
> `central-lite/relay_watcher/`.

---

## 1. The architecture today

Each neuron runs ONE network thread hosting ONE asyncio loop
(`_runNetworkClient`, `start.py:181-195`). On that loop:

- **One `SatoriNostr` client per relay URL.** `_networkConnect`
  (`start.py:380-411`) constructs `SatoriNostr(relay_urls=[relay_url])` and
  stores it in `_networkClients: dict[url -> client]` (`start.py:78`). One
  client = one nostr-sdk `Client` = one websocket.
- **Up to 7 listener tasks per relay** (observation, channel commitment,
  channel-open, settlement, tombstone, prediction, access-request; ensure
  helpers at `start.py:1196-1208`). For R relays: R sockets and 5-7 x R
  asyncio tasks.
- **One kinds-only REQ per client** (`client.py:1547-1557`): 8 event kinds,
  no `authors`/`#p`/`#d` constraint. Every neuron downloads every Satori
  event on the relay and discards what isn't for it client-side
  (`is_subscribed` drop at `start.py:948-953`, p-tag check at
  `client.py:1639-1641`).
- **Reconcile loop** (`_networkReconcileLoop`, `start.py:197`): despite
  docstrings saying "every 5 minutes" (`start.py:4061`) and "every hour"
  (`start.py:200`), the loop sleeps `3600` seconds (`start.py:281`). It
  re-establishes subscriptions, connects publisher relays, and is the only
  thing that revives dead listeners.
- **De-facto cleanup** is the periodic full process restart every 21-24 h
  (`start.py:4870-4878`).

```
TODAY (per neuron)                          TARGET (Phase 2+)

 start.py network thread                     start.py network thread
 +----------------------------+              +----------------------------+
 | reconcile loop (1h)        |              | reconcile loop (15m)       |
 |                            |              | supervisor task (30s)      |
 | _networkClients:           |              |                            |
 |  url1 -> SatoriNostr ------+- ws - R1     | ONE SatoriNostr            |
 |  url2 -> SatoriNostr ------+- ws - R2     |  \- nostr-sdk Client pool  |
 |  ...                       |              |      +- ws - R1            |
 |  urlR -> SatoriNostr ------+- ws - RR     |      +- ws - R2  (add/     |
 |                            |              |      \- ws - RR   remove   |
 | 7 listener tasks x R relays|              |            at runtime)     |
 | R dedupe caches (no cross- |              | 7 consumer tasks TOTAL     |
 |  relay dedupe)             |              | 1 dedupe cache (cross-     |
 | 1 kind-only REQ x R        |              |  relay for free)           |
 |  -> O(N^2) network egress  |              | 2 REQs: addressed (#p=me)  |
 |                            |              |  + observations (mode:     |
 | races, orphans, dead       |              |    targeted authors[] OR   |
 |  listeners for up to 1h    |              |    firehose kind-only)     |
 +----------------------------+              +----------------------------+
     R sockets, 7R tasks                        R sockets, ~8 tasks
```

### nostr-sdk API surface (verified)

`requirements.txt:67` pins `nostr-sdk>=0.44.0` (satorilib declares no
dependencies of its own — it rides the neuron image). The v0.44 FFI `Client`
exposes everything the later phases need:

| Capability | Methods |
|---|---|
| Runtime pool membership | `add_relay`, `remove_relay`, `force_remove_relay`, `remove_all_relays` |
| Per-relay connect | `connect_relay`, `disconnect_relay`, `connect`, `wait_for_connection`, `try_connect` |
| Subscriptions | `subscribe`, `subscribe_with_id`, `subscribe_to(urls, ...)`, `unsubscribe`, `unsubscribe_all`, `subscriptions()` |
| Targeted publish | `send_event_to(urls, event)`, `send_event_builder_to(urls, builder)` |
| Per-relay fetch | `fetch_events_from(urls, filter, timeout)`, `stream_events_from` |
| Health | `relays() -> {url: Relay}`, `Relay.status()/is_connected()/stats()` |
| Negentropy | `sync(filter, SyncOptions)` |

Recommendation: pin `nostr-sdk>=0.44,<0.45` before Phase 2/3 rely on these
signatures.

**Important correction to the "dead socket" theory.** The SDK's relay pool
auto-reconnects (default on) **and re-issues stored REQ subscriptions after
reconnect** (verified in rust-nostr v0.44, `connect_and_run -> resubscribe`).
Socket drops are handled below the application. Every observed failure mode
is application-layer, which is good news: all of them are fixable in our
code without touching the SDK.

---

## 2. Root causes of "unstable" and "connected extra"

1. **Duplicate-connection race.** `_networkConnect` is check-then-act with an
   `await` in the middle: dict check at `start.py:383`, `await
   client.start()` at `:390`, dict insert at `:391`. No lock. The hourly
   reconcile, web-triggered discovery (`POST /api/network/discover`), and
   `publishNowSync`/`announceNowSync` all schedule onto the same loop and
   interleave at every await. Two coroutines connecting the same URL both
   pass the check; the second insert overwrites the first client, which is
   never `.stop()`d — an orphan websocket nobody reads. This is the literal
   "connected extra".

2. **No URL canonicalization.** `_networkClients` is keyed on verbatim
   strings from three sources: central `server.getRelays()`, networkDB
   subscription rows, and the networkDB relays table. `ws://host` vs
   `wss://host`, trailing slashes, and host case all key separately — two
   sockets, two listener sets, duplicate observation processing against one
   physical relay. The code acknowledges the ws/wss mismatch
   (`start.py:302-303`) but only aligns the DB with central; it never
   canonicalizes.

3. **Silent listener death, slow revival.** Every per-relay listener is
   `async for ... except Exception: logging.warning('listener stopped')` and
   then exits (e.g. `_networkListen` at `start.py:959-963`). Nothing restarts
   it until an ensure-helper runs again — at most once per reconcile cycle
   (hourly), and observation listeners only re-ensure on a relay that
   re-qualifies in the hunt. A crashed listener can be dead ~1 hour. The
   client library has the same flaw one level down:
   `SatoriNostr._event_listener` (`client.py:1541-1574`) exits on exception
   while the SDK stays connected — "connected but deaf", since nothing pumps
   events into the queues anymore.

4. **Orphaned sockets on loop restart.** When the network loop crashes,
   `_runNetworkClient` restarts it after a random 60-600 s sleep
   (`start.py:184-195`), and `_networkReconcileLoop` clears every
   client/listener dict WITHOUT calling `.stop()` on the old clients
   (`start.py:218-227`). The dropped nostr-sdk clients keep live sockets
   until garbage collection.

5. **Publisher pinning, no prune.** `_neededRelays` (`start.py:3751-3764`)
   marks ALL known relays as needed whenever any publication exists, and
   `_networkEnsurePublisherConnections` (`start.py:293-323`) connects to
   every DB relay each cycle and never disconnects. Reconcile only
   disconnects relays it just connected for hunting and found nothing on
   (`start.py:4190-4192`). A relay that leaves central's list lingers until
   the next full restart.

6. **O(N^2) bandwidth.** The kinds-only REQ means aggregate relay egress
   scales as (number of subscribing neurons) x (total network events). The
   relay_watcher already hit the other side of this wall: putting thousands
   of authors into one REQ blew `maxWebsocketPayloadSize` and strfry silently
   dropped the socket (`relay_watcher/nip65.py:136-142`). Compounding
   issues: unbounded asyncio queues (`client.py:154-161`, no backpressure),
   a 50k-entry in-memory dedupe cache per client with no cross-relay sharing
   (`dedupe.py:19`), and regular (non-replaceable) kinds 34608 predictions /
   34609 access requests accumulating on relays for 3 years
   (`rejectEventsOlderThanSeconds = 94608000` in the strfry template,
   `relay_manager.py:691`).

Dead code confirming the intended-but-missing reliability layer:
`SatoriNostrConfig.active_relay_timeout_ms` and `dedupe_db_path` are never
read (`models.py:292-293`), `SQLiteDedupe` is a `NotImplementedError` stub,
`integrations/reliable_subscriber.py`'s reconnect loop is a stub comment, and
`_networkSubscribed` (`start.py:79, 392`) is initialized and cleared but
never populated. Meanwhile the working watchdog pattern exists in
`central-lite/relay_watcher/watcher.py:198-221` (staleness timeout 1800 s +
reconnect backoff 30 s) — ready to port.

---

## 3. Phase 1: tactical stability fixes

All independently shippable and separately revertible; ~220-280 lines total
across `start.py`, `client.py`, `network_db.py`, and one new helper. This
phase keeps the one-client-per-relay shape and makes it stable.

### 3.1 Per-URL connect lock

In `_networkConnect`, hold an `asyncio.Lock` per canonical URL across the
whole check -> `start()` -> insert sequence. Do NOT use a placeholder
sentinel in `_networkClients` — ~28 broadcast loops iterate `.values()` and
would trip over a non-client. Locks live in a
`self._networkConnectLocks: dict[url -> Lock]`, created on the network loop
and cleared with the other dicts on loop restart. ~20 lines.

### 3.2 URL canonicalization everywhere

New `canonical_relay_url(url)` helper in `satorilib/satori_nostr` (new
`urls.py`, ~30 lines): strip whitespace, lowercase scheme+host, drop default
ports, strip trailing slash. Identity is `host:port`, never host alone (two
different relays can share a hostname on different ports). For the ws/wss
split: canonicalize syntax everywhere, plus a reconcile-time alias pass — if
both schemes of the same `host:port` appear in the desired set, keep wss,
drop ws (embedded relays on `:7777` stay plain ws).

Apply at: `_networkConnect`/`_networkDisconnect` entry (defense in depth),
`_networkEnsurePublisherConnections` before `upsert_relay`
(`start.py:305-311`), `network_db.py:685 upsert_relay` and `:554
update_relay`, `_neededRelays`, and the hunt/discovery paths that read
subscription rows. Plus a one-time networkDB startup migration merging
existing duplicate rows (~20 lines).

### 3.3 Listener supervisor task

New `_networkSupervisor()` coroutine spawned alongside the data-source
manager: every 30 s, for each connected client, call the existing idempotent
`_networkEnsure*` helpers (they already no-op when the task is alive) with
per-(url, listener) exponential backoff (5 s -> 300 s) and a death counter
that logs loudly on repeated crashes. Listener death goes from
"silent, dead up to an hour" to "restarted within 30 s, visible in logs".
~50 lines.

### 3.4 Staleness watchdog + forced reconnect (port from watcher.py)

- `client.py` (~15 lines): stamp `self._last_event_at` at the top of
  `_handle_event`; expose `seconds_since_last_event()` and
  `relay_connected()` (via `Relay.is_connected()`). Also make
  `_event_listener` re-enter `handle_notifications` after a short sleep on
  non-cancel exceptions instead of exiting — this alone removes the
  "connected but deaf" client-level failure.
- `start.py` (~30 lines): in the supervisor, if a relay reports
  disconnected for > 120 s, or has been silent > 1800 s WHILE other relays
  are receiving events (guards against reconnect storms on a quiet network),
  `_networkDisconnect` + `_networkConnect` with 30 s per-URL backoff.

### 3.5 Graceful client stop on loop restart

Add `_networkShutdownAll()` (cancel all listener dicts' tasks, then
`await asyncio.wait_for(client.stop(), 10)` per client, then clear dicts) to
the reconcile loop's `finally`. The existing dict-clear preamble
(`start.py:218-227`) stays as a safety net for paths that never reach
`finally`. Shares the 7-dict teardown boilerplate with `_networkDisconnect`.
~35 lines.

### 3.6 Reconcile prune step

After publisher connections each cycle:

```python
needed = {canonical_relay_url(u) for u in self._neededRelays()}
if self._firehoseEnabled:           # FIREHOSE.md keeps all central relays
    needed |= central_listed_relays
for url in set(self._networkClients) - needed:
    await self._networkDisconnect(url)
```

Removed-from-central relays, dead user-added relays, and ws/wss alias losers
finally get disconnected. Must land with (or after) 3.2, or a stale-form URL
in a subscription row could prune its own relay. ~10 lines.

### 3.7 Cadence fix

`asyncio.sleep(3600)` at `start.py:281` -> `900` (15 min). The supervisor
covers the fast path at 30 s, so reconcile no longer doubles as a health
checker. Fix the contradictory docstrings (5 min at `start.py:4061`, hourly
at `:200`).

### 3.8 Bounded queues

`client.py:154-161`: all 8 queues get `maxsize=10_000`; producers switch to
drop-oldest with an `events_dropped` stats counter (blocking would stall the
single notification pump for every kind). Drop-oldest is semantically aligned
for observations — kind 34601 is replaceable, latest wins. Surface the
counter in the web UI health panel so overload is observable.

### Phase 1 verification (two-container `satori` + `satori-2`)

- **Race repro**: seed the DB with `ws://relay:7777` and central with
  `wss://relay:7777`; before the fix, two sockets to one relay; after, one.
  Trigger web discover twice rapidly during a reconcile — exactly one
  "Network: connected to" per canonical URL.
- **Listener recovery**: kill the relay container mid-run or inject an
  exception into `_networkListen`; supervisor restarts it within 60 s and
  observations resume without waiting for reconcile.
- **Staleness**: pause the relay ~35 min (or drop the timeout in dev);
  expect forced disconnect + reconnect and resumed delivery.
- **Graceful stop**: raise inside the reconcile loop; every relay logs a
  disconnect before the restart, and `lsof | grep -c 7777` returns to
  baseline (no orphans).
- **Prune**: remove a relay from central + DB; it disconnects within one
  reconcile cycle.

---

## 4. Phase 2: one pooled SatoriNostr per neuron

nostr-sdk `Client` natively pools relays — `SatoriNostr.start()` already
loops `add_relay` over `config.relay_urls` (`client.py:208-209`); the neuron
just never passes more than one. Collapsing to one client per neuron gives:
one `_event_listener`, one REQ set, one dedupe cache (cross-relay dedup for
free — same event id from R relays delivers once), and 7 consumer tasks
TOTAL instead of 7 x R. The supervisor shrinks to 7 tasks + per-relay health
via `relays()`.

### 4.1 satorilib changes (~80-120 lines)

- `add_relay(url)` / `remove_relay(url)` / `relay_health()` wrappers on
  `SatoriNostr` (pool membership mutates at runtime; SDK reconnects and
  re-REQs per relay automatically).
- A `relay_urls: list[str] | None` kwarg on the handful of publish methods
  that need per-relay targeting, routed to `send_event_builder_to` — rather
  than 17 new methods.
- Every `Inbound*` model gains `relay_url`, populated from the notification
  handler (it already receives `relay_url` per event, `client.py:1563`) —
  the hunt path and per-relay stats need attribution.
- Bump the now-shared dedupe cache (50k -> 200k; ~13 MB of hex ids).

### 4.2 Neuron migration — rewrite, don't shim

**The one real hazard**: ~28 broadcast loops
(`for client in self._networkClients.values(): await client.announce_x(...)`)
must be REWRITTEN to single pool calls. A transparent facade that makes the
dict yield the pool R times would republish every event R x to every relay
(every publish in `client.py` uses `send_event_builder`, which broadcasts to
the whole pool). Mechanical rewrite:

- Broadcast loops -> one pooled publish. Semantics change from "R sequential
  publishes" to "one publish with per-relay success/failed output" —
  acceptable everywhere, since every existing loop ignores per-relay
  failures (`try/except continue`).
- ~10 targeted `.get(relay_url)` sites: most only exist because clients are
  per-relay and plain broadcast is fine (tombstones, settlements are
  addressed events, extra copies dedupe at consumers). The genuinely
  targeted ones (announce-on-this-relay after connect, per-relay discovery)
  use the `relay_urls=[url]` kwarg / `fetch_events_from`.
- The 7 per-relay listener dicts become 7 plain task attributes; they
  already consume per-client queues, so with one client they collapse
  naturally. `_networkConnect`/`_networkDisconnect` become pool
  `add_relay`/`remove_relay` plus needed-set bookkeeping.

### 4.3 Rollout

1. Ship the satorilib additions (backward compatible; single-relay
   construction still works).
2. Config flag (`network: pool_mode`), default off: reconcile drives either
   the legacy per-relay implementation or the pooled one. Two-container
   soak: one neuron pooled, one legacy, cross-subscribed — wire-identical
   except duplicate-event reduction.
3. Flip the default after soak; delete the legacy path a release later.

### 4.4 Verification

One websocket per relay (`lsof` in container); events received once (dedupe
counter); a publish lands on all pool relays (strfry scan); kill one relay ->
others unaffected, restore -> SDK reconnects with no neuron intervention;
`len(asyncio.all_tasks())` drops from ~7R to ~8.

---

## 5. Phase 3: bandwidth scaling (filters and retention)

### 5.1 Split the kind-only REQ

Replace the single 8-kind filter (`client.py:1547-1557`) with two
subscriptions via `subscribe_with_id`:

**A. Addressed filter — always on, every node type:**

```python
Filter().kinds([PAYMENT, CHANNEL_COMMITMENT, CHANNEL_OPEN,
                CHANNEL_SETTLED, PREDICTION, ACCESS_REQUEST])
        .pubkey(me)          # '#p' = my pubkey, filtered server-side
```

All six kinds already carry `p` tags (verified across the publish sites in
`client.py`), so this is a config-free, protocol-free change that stops every
neuron from downloading every other neuron's payments, channel traffic,
predictions, and access requests. Audit item before shipping: consumers of
events NOT p-tagged to them (e.g. the tombstone listener watches for any
tombstone on channels it knows) — keep those specific kinds broad or add a
`#d` filter on known channel addresses.

**B. Observation filter — mode per config
(`observation_filter_mode: firehose | targeted`):**

- `firehose`: today's kind-only 34601 filter. Required by FIREHOSE.md — MV
  nodes deliberately consume the full stream fan-out.
- `targeted`: `.authors([subscribed provider pubkeys])` — one author covers
  all of that provider's streams. Chunk at <= 150 authors per REQ (strfry
  `maxReqFilterSize = 200`); `maxSubsPerConnection = 20` minus 2 base subs
  leaves headroom for ~2,700 authors; 150 x 64-hex is ~10 KB, far under
  `maxWebsocketPayloadSize = 131072` (the relay_watcher overflow was
  thousands of authors in ONE REQ — chunking avoids it). New client API
  `set_observation_authors(pubkeys)` re-chunks on subscription-set changes;
  the SDK re-REQs the stored subscriptions on reconnect.

### 5.2 Retention of regular kinds

Predictions (34608) and access requests (34609) accumulate for 3 years. In
preference order: (1) NIP-40 `expiration` tags on publish; (2) move
predictions to an ephemeral kind (20000-29999; strfry already configures
`ephemeralEventsLifetimeSeconds = 300`) since hosts consume them live;
(3) regardless, a relay-side cron purge
(`strfry delete --age=7d --filter='{"kinds":[34608,34609]}'`) in
`relay_manager.py` and central relay ops — cheapest immediate fix, ship it
first. Longer-term: make predictions parameterized-replaceable with
`d=stream:round` so only the latest per (predictor, stream, round) persists.

### 5.3 Quantified win

With N publishing neurons (~1 obs/min, ~1 KB), S subscribers, k average
subscribed providers, F firehose nodes:

- Today: aggregate relay egress ~ S x N KB/min. At N=S=1000 that is
  ~1 GB/min across the fleet, and each neuron downloads ~1 MB/min of which
  ~99% is discarded at the `is_subscribed` check.
- Targeted: (S-F) x k + F x N. With k=10, F=50: ~60 MB/min — **~17x less
  aggregate**, and non-firehose neurons drop from O(N) to O(k), ~100x less
  each. The addressed filter (A) alone is a large win with tiny code.

### 5.4 Verification

Targeted mode on `satori`: an unrelated free stream published from
`satori-2` produces zero 34601 arrivals (stats counter), while the
subscribed stream flows; flip to firehose mode and the unrelated stream
arrives. Measure container network I/O under a 10-stream synthetic load
before/after.

---

## 6. Phase 4 (directional): relay-side and network scaling

- **Connection caps.** The strfry template (`relay_manager.py:691`) has no
  per-IP or total connection cap and strfry has no native per-IP limit —
  front public relays with nginx/haproxy `limit_conn`, and set `nofiles`
  deliberately instead of `0`.
- **Relay assignment / sharding.** Central assigns each neuron a relay
  subset (rendezvous hashing of neuron pubkey over the healthy-relay
  directory the relay_watcher already maintains), delivered through the
  existing `server.getRelays()`. `_neededRelays` changes from "all relays if
  publishing" to "assigned relays". Providers already publish NIP-65
  (`start.py:359-378`), so hunters know where to look — the outbox model
  lands here. Firehose/MV nodes opt out and stay all-relay, acting as
  network-wide aggregators.
- **Negentropy catch-up.** `Client.sync()` is available and strfry has
  negentropy enabled (`maxSyncEvents = 1000000`) but no app code uses it.
  Use it on reconnect/startup for regular kinds; replaceable 34601s hold
  only latest-per-(author, d), so observation history sync stays pointless
  until publisher-served history exists (FIREHOSE.md follow-up).
- **Embedded relay roles.** Public embedded relays (mode `public`, port
  7777) become the shard substrate; central's watcher stats demote flaky
  ones and diversify assignments.

---

## 7. Interaction with FIREHOSE.md

| FIREHOSE.md needs | This design provides |
|---|---|
| Connect to ALL central relays | The Phase 1 prune step (section 3.6) unions the firehose relay set into `needed`; Phase 4 sharding exempts firehose nodes |
| Kind-only 34601 REQ ("relays already deliver everything") | Phase 3's `observation_filter_mode: firehose` (section 5.1) preserves it per node; the addressed filter still saves firehose nodes everyone else's payments/predictions |
| Per-relay `_networkListen` ingest hook | Phase 2 collapses listeners to one consumer; the ingest point (the `is_subscribed` drop) lives in the consumer body and survives unchanged |
| More relays, more streams | Phase 1 is a precondition: firehose multiplies relay count, which multiplies today's orphan/race/dead-listener rates |

---

## 8. Risk register

1. Canonicalization must key on `host:port`, never host alone — two real
   relays can share a hostname on different ports.
2. Phase 2's R x publish duplication if any `.values()` broadcast loop
   survives the rewrite — add a CI grep for `.values()` over the pool.
3. Targeted filters silently starving an unaudited consumer of
   un-p-tagged events — an audit table of all 8 `_handle_*` paths is a
   Phase 3 exit criterion; ship filter A behind a flag with kind-only
   fallback.
4. Drop-oldest queues masking a stuck consumer — the `events_dropped`
   counter must be surfaced, not just counted.
5. `nostr-sdk>=0.44.0` is a floor, not a pin — cap at `<0.45` before
   relying on `send_event_builder_to`/`sync` signatures.
6. Supervisor + watchdog reconnect storms on genuinely-down relays — the
   per-URL exponential backoff and the "other relays receiving" staleness
   guard are load-bearing; keep them.
