# Relay Firehose: Feed ALL Free Nostr Streams to the MV Engine

Companion to [`MULTIVARIATE.md`](./MULTIVARIATE.md) and
[`Jordan-1_MULTIVARIATE.md`](./Jordan-1_MULTIVARIATE.md). The multivariate
adapter is implemented and picks peer streams from the engine's shared
`StreamStore` — but today that store only receives streams the node
**predicts**, so the peer pool is nowhere near "all the streams on the
relays". This doc designs the smallest change that fixes that: a passive
firehose that ingests every free relay stream, data-only, into the store.

> Status: design only, not yet implemented. All file/line references verified
> against the current code.

---

## 1. The gap

How observations reach the engine today:

- Plain subscriptions stop at `networkDB`; the engine path is gated by
  `networkDB.is_predicting(...)` in `_networkProcessObservation`
  (`neuron-lite/start.py:507-511`). Subscribed-but-not-predicting streams
  never reach the `StreamStore`.
- Non-subscribed observations are dropped outright in `_networkListen`
  (`start.py:948-953`, the `is_subscribed` check).

So the MV adapter's candidate pool (`store.stream_uuids()`) is effectively
"streams this node predicts" — a handful, chosen manually.

`MULTIVARIATE.md` roadmap item 5 proposed a warm pool that auto-subscribes to
top-N discovered streams. That design is superseded by the observation below:
it would be capped by `getMaxTotalStreams()` (100 combined pubs+subs,
`network_db.py:23`), it publishes per-stream subscription announcements, and
it would *still* need a new data-only ingestion path, since subscribing alone
does not feed the engine store.

## 2. The key fact: relays already deliver everything

The Nostr client subscribes to relays **by event kind, not by stream**
(`satorilib/satori_nostr/client.py:1547`): every kind-34601 observation event
on a connected relay is parsed and queued by `client.observations()`.

- Free streams are plaintext JSON (`client.py:1632-1634`) — readable by
  anyone connected, subscriber or not.
- Paid streams are NIP-04 encrypted per subscriber and skipped at the client
  unless addressed to us (`client.py:1636-1641`). They exclude themselves.

The only thing standing between the node and "all free relay streams" is the
one-line `is_subscribed` drop in `_networkListen`. No subscription
announcements, no cap usage, no discovery round-trips needed for data
acquisition — just connect to more relays and stop dropping.

What this does NOT change: relays hold only the latest observation per stream
(replaceable events, see MULTIVARIATE.md §5.1), so firehose streams still
accumulate history only from first sight. Backfill (publisher-served history)
remains a separate future mechanism.

## 3. Design

**Passive firehose**: at the `_networkListen` drop point, ingest free,
numeric, non-`_pred` observations straight into the engine `StreamStore`
(data-only: no `StreamModel`, no prediction, no `networkDB` rows, no
subscription announcement). Connect to all central-listed relays so the node
sees everything. Active automatically when
`engine.preferred_adapter == 'multivariate'`, with a config override.

All changes live in `neuron-lite/start.py` (~75 lines). Zero engine-lite
changes — `StreamStore`, the storage manager, and the MV adapter already do
everything needed.

### 3.1 State init

In `StartupDag.__init__` (near `self._networkClients`):

```python
self._firehoseEnabled: bool = False
self._firehoseMaxStreams: int = 500
self._firehoseAdmitted = None  # Optional[set[str]], lazily seeded from store
```

### 3.2 Config read: `_firehoseRefreshConfig()`

Reads `engine.multivariate.warm_pool.{enabled, max_streams}` from
`config.get()`; `enabled` defaults to `preferred_adapter == 'multivariate'`
when absent. Called at the top of `_networkReconcileLoop` and once per hourly
iteration, so config edits apply within an hour.

Defaults live neuron-side, inline. NOT in `multivariate.py:_DEFAULTS`: the
adapter never reads `warm_pool`, its config merge only copies keys it
consumes, and the neuron should not import engine adapter internals for
config.

```yaml
engine:
  preferred_adapter: multivariate   # firehose auto-enables with this
  multivariate:
    warm_pool:
      enabled: true        # optional override, force on/off regardless of adapter
      max_streams: 500
```

### 3.3 Ingest hook in `_networkListen` (start.py:952)

```python
if not subscribed:
    await self._firehoseIngest(obs)
    continue
```

### 3.4 `_firehoseIngest(obs)` + `_firehoseStore(uuid, df)`

Guards, in order, each an early return:

1. Firehose disabled, or `obs.observation` is None.
2. Stream name empty or ends `'_pred'`. **Load-bearing**: other nodes'
   prediction publications are also free plaintext streams, and relay pred
   uuids hash the `_pred`-suffixed NAME into opaque hex
   (`relay_ids.py:60`), so `eligiblePool`'s uuid-suffix filter
   (`peer_search.py`) can never catch them. This name check is the only
   guard against prediction-of-prediction circularity.
3. Own streams: `obs.nostr_pubkey == self.nostrPubkey` (start.py:76).
4. `float(obs.observation.value)` fails, or not `math.isfinite`.
5. `self._safeEpoch(obs.observation.timestamp)` (start.py:1056, bounds
   2000-2100) returns None.
6. `_firehoseAdmit(uuid)` returns False (§3.5), where
   `uuid = relay_uuid(name, obs.nostr_pubkey)` from
   `satorineuron.relay_ids` — the same deterministic uuid the prediction
   path would use, so a later "predict this stream" decision inherits the
   accumulated history for free.

Then build the exact frame `StreamStore.append` expects
(`stream_store.py:150`):

```python
df = pd.DataFrame([{'epoch': epoch, 'value': value,
                    'id': str(obs.observation.seq_num)}])
await asyncio.to_thread(self._firehoseStore, uuid, df)
```

where `_firehoseStore` does
`EngineStorageManager.getInstance().storeStreamData(uuid, df)` with
`from satoriengine.veda.storage import EngineStorageManager`.

**No `ensureEngine()` anywhere.** The storage manager is a process-wide
singleton whose defaults (`storage/manager.py:33`) are the same db the
`Engine` attaches to (`engine.py:161`). Writing through it directly solves
startup ordering (listeners can run before the engine exists) and avoids
constructing an `Engine` when the firehose is the only consumer. Dedup is
free via the `(stream_uuid, epoch)` primary key (`INSERT OR IGNORE`). No
per-ingest info logging (debug at most) — thousands of streams would spam.

### 3.5 `_firehoseAdmit(uuid)`: the cap, cheaply

In-memory set, lazily seeded once from
`asyncio.to_thread(... stream_store.stream_uuids())` — no store query per
observation:

- uuid already in set → True.
- set size >= `_firehoseMaxStreams` → False (new streams ignored at cap).
- else add and True.

Restart persistence falls out for free: re-seeding from the store re-admits
every stream that ever stored a row. Seeding includes central/predicted
uuids counting toward the cap — conservative and simple. First-come
admission; dead-stream eviction is an explicit follow-up (§6), not v1.

### 3.6 Connect to all relays: `_networkConnectAllRelays(ConfigClass)`

In `_networkReconcileLoop`, right after the `_networkReconcile` step
(~start.py:252):

```python
self._firehoseRefreshConfig()
if self._firehoseEnabled:
    await self._networkConnectAllRelays(SatoriNostrConfig)
```

The method: `relays = await asyncio.to_thread(self.server.getRelays)` (the
same call `_networkReconcile` uses, start.py:4117), then
`await self._networkConnect(r['relay_url'], ConfigClass)` per relay.
`_networkConnect` (start.py:380) is idempotent and auto-starts
`_networkListen` for the relay (start.py:402-405), so firehose listeners
come for free.

This must be a separate step, not folded into `_networkReconcile`: that
function early-returns when no subscriptions are stale (start.py:4069,
4095), which would starve the firehose of connections.

### 3.7 Subscribed-but-not-predicting streams (2 lines)

In `_networkProcessObservation`, add an `else` to the `if predicting:`
branch (start.py:510-514):

```python
else:
    await self._firehoseIngest(obs)
```

Today those observations reach only `networkDB`, invisible to MV. Predicted
streams already land in the store via `_relayPredict`; any overlap dedups on
`(stream_uuid, epoch)` (both paths derive epoch from the same
`observation.timestamp` through `_safeEpoch`).

## 4. Threading and volume

- `_networkListen` is async; the SQLite write goes through
  `asyncio.to_thread`, and `StreamStore` has its own lock, so concurrent
  listeners across relays are safe.
- One `INSERT OR IGNORE` + commit per observation is a trickle at relay
  cadences (seconds-to-minutes per stream), even at 500 streams. Batching is
  a follow-up only if profiling demands it.
- MV fit-time cost is already bounded independently of store size:
  `max_candidates` (50) caps peer-history loads, and `condition()`'s stream
  count is one SQL query behind a ~60s TTL cache.

## 5. Verification (two-container dev setup: `satori` + `satori-2`)

1. On `satori`: set `engine.preferred_adapter: multivariate`, restart the
   container.
2. On `satori-2`: publish a free relay stream that `satori` is NOT
   subscribed to.
3. `satori` logs: relay connect lines from `_networkConnectAllRelays`, then
   observations arriving on `_networkListen` without being dropped.
4. Store check:
   ```
   docker exec satori python -c "
   from satoriengine.veda.storage import EngineStorageManager
   s = EngineStorageManager.getInstance().stream_store
   print(s.stream_uuids(), s.count_streams_with_min_rows(1))"
   ```
   Expect the uuid matching `relay_ids.relay_uuid(name, satori2_pubkey)`;
   `row_count` grows per observation.
5. MV visibility: once >= 2 streams have >= 30 rows, `condition()` flips
   true; a fit's candidate pool (`store.stream_uuids()` ranked by
   `row_count`) includes the firehose uuid.
6. Negatives: a `*_pred`-named stream is not ingested;
   `warm_pool: {enabled: false}` disables despite the MV adapter; a
   non-numeric stream's values are skipped.

## 6. Risks, edge cases, follow-ups

Handled in v1:

| Case | Handling |
|---|---|
| Listener runs before engine exists | Storage-singleton writes only; no `ensureEngine()` |
| Duplicate ingestion (predicted/subscribed overlap) | `(stream_uuid, epoch)` PK dedup; same `_safeEpoch` source |
| Non-finite / non-numeric values | `float()` + `math.isfinite` guard |
| Publisher clock skew | `_safeEpoch` bounds (2000-2100); in-range future timestamps tolerated by MV alignment's future-row guard |
| Runtime `preferred_adapter` change | Picked up next hourly cycle; disabling stops new ingests, stored data stays (harmless); open relay connections persist until restart (harmless) |
| Paid / encrypted streams | Excluded at the client (undecryptable) |
| Own streams, `_pred` streams | Name/pubkey guards in `_firehoseIngest` |

Explicit follow-ups, out of scope for v1:

- **Dead-stream eviction**: the admitted set is first-come; a stream that
  goes silent holds a slot forever. Add staleness-based eviction (drop from
  the set, optionally prune rows) when the cap starts binding.
- **Row retention**: `StreamStore` never prunes; at 500 streams growth is
  modest, but a keep-last-N-rows pruner is cheap insurance if `engine.db`
  size ever matters.
- **History backfill**: firehose streams start at zero history. The
  publisher-served history protocol (MULTIVARIATE.md §5.1 option 2) is the
  lever that removes the accumulation wait; unchanged by this design.
- **UI visibility**: firehose streams appear nowhere in the web UI (they are
  not subscriptions). A read-only "engine data pool" listing is a possible
  later addition.
