# Base Data Streams over Nostr

**Date:** 2026-08-26
**Status:** Environment and value handling complete (branch `base-streams`,
commit `fcbee0c`); waiting on the bridge to publish its first events


## Summary

Let a neuron subscribe to data read from smart contracts on Base (Coinbase's
Ethereum L2) and predict on it, the same way it predicts on any other stream.

The neuron is a **subscriber only**. An external bridge reads the contracts,
signs with one Nostr keypair, and publishes plaintext kind 34600/34601 events.
No `eth_call`, no ABI decoding and no RPC belongs on the neuron side.

This is deliberately **not** a new kind of stream. It arrives through the same
`_networkListen` firehose, parses with the same `DatastreamObservation`, lands
in the same `observations` table, gets the same uuid5 from `relay_ids.py`,
trains the same `StreamModel` and emits the same `{stream}_pred`. There is no
new event kind, no new source type and no parallel pipeline. The only thing
that differs is who publishes it.


## Division of labour

The bridge and the contracts are owned by the senior; the neuron is ours.
Nostr relays are the only shared surface, so the entire interface between the
two sides is three things: the publisher key, the relay, and the payload
schema. Everything else on either side is independent.

```
THEIR SIDE                  SHARED                     OUR SIDE
Base contracts              Nostr relay                subscribe, pin the key
the bridge          ---->   kinds 34600 / 34601  --->  ingest, dedupe, predict
signs with the key          the payload schema         publish {stream}_pred
```


## Naming and shape

Streams are named `satori-<chainId>-<streamId>`. On Base Sepolia (84532),
`satori-84532-1` is the base stream and `-2`, `-3`, `-4` are roll ups at 28,
183 and 731 rounds. A round is 43200 seconds (12 hours), so the roll ups are
roughly 14 days, 3 months and a year.

A kind 34601 observation carries an **object**, not a scalar, because one
observation describes an on chain write:

```json
{
  "stream_name": "satori-84532-1",
  "timestamp": 1787313600,
  "seq_num": 1,
  "value": {
    "chainId": 84532, "streamId": 1,
    "block": 45772737, "round": 41373, "raw": "1"
  }
}
```

Four rules from the publisher's spec:

1. `raw` is authoritative. Always a string, an `int256`, signed (negatives
   occur), and wider than a float holds exactly.
2. `value` may be absent. It is present only when a real conversion existed
   (a tick turned into a price), alongside `valueUnit`. **Absent is not zero.**
3. `timestamp` is the round start (`round * 43200`), not publish time.
4. `unit` is `raw`, or `tick` for Uniswap streams.


## The publisher key, and what it is for

Nostr has no accounts and no login. Every event carries a signature and the
public key is the identity.

The bridge holds the private half and signs everything it publishes. The
neuron holds only the public half and never signs with it. It uses it as a
filter, because **stream names are not owned**: nothing stops anyone
publishing an event that claims `stream_name: satori-84532-1`, and relays
will accept and forward it. Without pinning the key a neuron would ingest
whatever anyone published under that name, train on it, and pay bounties
against it.

The pin already exists in the code. `NetworkDB.is_subscribed(stream_name,
provider_pubkey)` matches on both, and `provider_pubkey` is compared against
the **event's author**, so anything signed by a different key is dropped in
`_networkListen` (`start.py:944`).

Current key: `8ddb04208aaf271fd70b397a7aa7ac79b95f4dcdcf9b6e22b64c9d3f90a8c777`
(npub `13hdsggy24un3l4ct89a84fav0xu47nwde7dkug4kfjwnly9gcamsl4vd5s`).

This is the **permanent publisher**, generated 2026-08-26 and held in the
bridge's deployment env. An earlier draft of this document recorded
`472e6f68…` — that was a throwaway used to test the bridge, its private half
was exposed in a terminal, and nothing should pin it. Any subscription row
created against it must be updated.

Two consequences worth recording:

- The key is stored in the `subscriptions` row created when you subscribe, not
  in config and not in code. Changing it is a one row update, which is cheap
  today because nothing else pins it, and stops being cheap the moment a
  second neuron subscribes. Rotation is a coordinated migration, not a
  restart, and Nostr has no revocation.
- If the private half leaks, whoever holds it can publish fraudulent Base
  readings that every subscriber accepts as genuine.


## Resolved: the relay question needs no code

The original design assumed the bridge would run its own relay that central
had never heard of, and added a pinned connection path to reach it. That work
is **not needed**.

A neuron already connects to every relay central advertises. The four rows in
the local DB (all `user_added=0`, meaning they came from central):

```
ws://satorian.satorinet.io:7777     <- resolves, answers NIP-11 ("Satori Public Relay")
ws://testnet.satorinet.io:7171      <- NXDOMAIN
ws://testnet.satorinet.io:7777      <- NXDOMAIN
wss://satori-home.net-hub.de        <- resolves (IPv6)
```

**`testnet.satorinet.io` no longer exists in DNS** — checked against Cloudflare's
public resolver (Status 3, NXDOMAIN) on 2026-08-27, not just a local cache. It
was live as recently as April (central's relay-watcher logs reference it), so
these are stale directory entries: central's list can hold dead relays, and a
row in the DB is not evidence a host answers. The natural home for Base Sepolia
streams is therefore **`ws://satorian.satorinet.io:7777`**, the one
central-advertised relay verified up.

If the bridge publishes to a live one of them, the existing machinery does the
whole job:

1. `_networkReconcile` connects to central's relays (`start.py:4117`).
2. `_networkConnect` starts `_networkListen` on each (`start.py:402`).
3. `discover_datastreams` picks up the kind 34600 announcement, so the stream
   appears in the UI.
4. Subscribe, then Predict, through the existing UI.
5. `is_subscribed` pins the publisher on every event that arrives.

New code required on that path: none.

**The caveat to pass on:** central does not learn about relays by magic. It
builds its directory from NIP-65 (kind 10002) announcements by registered
peers. A relay nobody registers is invisible to every neuron on the network,
which is why `relay.satorinet.io` would not have worked even once its DNS
resolved.

Consequently these are dropped from the plan entirely:

| Dropped                                    | Why |
|--------------------------------------------|-----|
| Config keys for the bridge pubkey and relay | The pin lives in the `subscriptions` row |
| A dedicated bridge connection routine       | Reconcile already connects to central's relays |
| Excluding pinned streams from reconcile     | Only existed to protect a stream reconcile could not find |


## Completed

### Environment (branch setup, no product code)

`satorilib` and `satori-lite` are both on `base-streams`. satorilib is
`origin/main` (the nostr-sdk 0.45 client plus two electrumx fixes) with the
wallet lock commit cherry picked on top.

Two traps worth recording for whoever sets this up next:

- The dev image shipped **nostr-sdk 0.44.2**, and satorilib's migrated client
  cannot import on 0.44.x. Bind mounts change source, not installed packages,
  so switching branches alone crashes with `cannot import name
  'HandleNotification'`. Changing `requirements.txt` invalidates the Docker
  layer at `Dockerfile:44`, and the torch plus timesfm layer at `:55` sits
  after it, so a full `./build.sh dev` re downloads several GB. A thin overlay
  (`FROM satorinet/satori-lite:dev-pre045` plus a pip install) rebuilds in
  seconds.
- **`pytest` is not installed in the dev image at all**, despite
  `Dockerfile:49`. The `docker exec satori python -m pytest` command in
  CLAUDE.md therefore cannot work as written. Fix the Dockerfile when
  convenient.

### Value extraction (`fcbee0c`)

Bridge observations carry an object, so `float(observation.value)` raised,
`numeric_value` became `None`, and control fell into the echo branch. The
neuron would have subscribed, received and stored every Base observation
correctly and **never predicted on one**, with no error anywhere. The only
tell was a log line reading `(echo)` instead of `(engine)`.

`StartupDag._numericObservationValue` now unwraps it, and is shared by the
three places that each did their own `float()`:

- the engine entry (`start.py:980`)
- the history backfill (`start.py:1201`)
- bounty scoring (`start.py:555`), which was silently skipping rather than
  crashing, so it was quietly wrong too

It prefers the converted `value` when present and falls back to `raw`. An
absent `value` means no conversion existed and is never treated as zero,
because publishing a fabricated zero into a price stream would poison every
subscriber scoring against it. One refinement since the first cut: when the
raw number is a **tick** (`unit: "tick"`) and no converted value came with it,
the helper refuses it rather than falling back — a tick is the logarithm of a
price, and mixing raw ticks with converted prices in one series would corrupt
the model the moment the bridge's RPC config changed. Such observations echo.

Verified with 15 tests covering zero, negative, absent value, garbage and
magnitudes above 2^53, and against real testnet data: all five rounds of
`satori-84532-1` take the engine path.


## Remaining work

### 1. Idempotency on (stream_name, round) — ✅ DONE

Implemented as designed: a nullable `round` column (try/ALTER migration, so
existing databases keep their rows), populated from the value object on the
single ingest path, deduped on `(stream_name, provider_pubkey, round)` when
not null and checked before the event-id and seq guards. Ordinary streams pass
no round and behave exactly as before. Covered by `TestRoundDedupe` and an
ingest-path replay test.

### 2. Staleness on the derived streams — ✅ RESOLVED (bridge side)

The analysis was right: cadence on these streams is a **minimum, not a
schedule**, and `is_likely_active` reads `cadence_seconds` as a schedule. The
fix landed where the mismatch was created: the bridge no longer emits
`cadence_seconds` at all (the on-chain floor still travels in
`metadata.derivedCadenceRounds` for anyone who wants it). Null cadence means
`is_likely_active` returns true unconditionally, same as every other Satori
stream, so no neuron-side guard is needed.

### 3. Indexer backfill (optional)

Kind 34601 is parameterized replaceable, so a relay holds only the latest
observation per stream and offline time is lost. This is **not special to
these streams**: it is true of every Satori Nostr stream. What is new is that
this publisher has an indexer to recover from.

`GET /streams/:id/values?fromBlock=N` at `https://app.satorinet.io/api`
(verified live). Mirror the idiom at `start.py:1971`: a nested sync function
using `_requests.get(..., timeout=15)`, `raise_for_status()` and `.json()`,
called through `await asyncio.to_thread(...)`. Push results through
`_networkProcessObservation` so dedupe, storage and the engine path behave
identically to live delivery.

The indexer returns `oldValue` and `newValue` while the Nostr payload carries
`raw`. Confirm the mapping against real events rather than assuming it.


## Testing

`satori-dev` does not mount `tests/` (`satori-dev:85`), and the copy baked in
at `Dockerfile:60` is stale. Use a throwaway container with the real repo
layout:

```bash
docker run --rm -v /path/to/repos:/work -w /work/satori-lite \
  -e PYTHONPATH=/work/satorilib/src:/work/satori-lite/neuron-lite:/work/satori-lite/engine-lite:/work/satori-lite \
  satorinet/satori-lite:dev python -m pytest -q tests/test_network_pipeline.py
```

**Test isolation is broken and predates this work.**
`tests/test_network_pipeline.py` leaks `sys.modules` stubs, so running several
network test files together produces different results run to run (12 then 10
failures were observed from identical inputs), and
`tests/test_network_reconcile.py` fails collection outright even when run
alone. Always compare against a stashed baseline of the same files in the same
combination rather than trusting an absolute pass count.

Live check once the bridge publishes: the stream appears in the UI after
discovery, Subscribe then Predict, then watch `./satori-dev logs` for a
prediction line reading `(engine)` and **not** `(echo)`, followed by a
`{stream}_pred` publish.


## Open questions for the bridge author

1. **Publish to `ws://satorian.satorinet.io:7777`.** Central already
   advertises it, neurons are already connected, and it is the only
   central-advertised relay that currently resolves (`testnet.satorinet.io`
   is NXDOMAIN — see the relay section). No relay needs standing up and no
   neuron code changes. See the caveat about NIP-65 registration above.
2. **Are the values binary by nature?** Stream 1 reads 0, 0, 1, 0, 0. The
   engine is a regression stack scored by MAE, which handles a 0/1 series
   badly: a constant guess of 0.2 beats most honest attempts. If it is binary
   we need a different scorer before any forecast is meaningful. See "Known
   issue" below.
3. **What does the number represent?** Nobody can judge a prediction without
   this.
4. **Key rotation.** Cheap now, expensive once a second neuron subscribes.

Deliberately not asked, because they become observable once events flow:
whether `seq_num` is reused on republish, whether `raw` equals the indexer's
`newValue`, when `value` and `valueUnit` appear, and whether the round gaps
(41376, 41377, 41379, 41380) mean "no on chain write happened" or "lost".


## Known issue this surfaces

If these streams are genuinely binary, MAE scoring is the wrong tool. This is
already recorded in the developer roadmap: `scoring/` contains only `mae.py`,
and classification streams have no scorer that matches them. Out of scope
here, but it determines whether a forecast on these streams means anything.


## Data volume caveat

At the time of writing, `satori-84532-1` holds five values (0, 0, 1, 0, 0
across rounds 41373 to 41381, with gaps) and the three roll ups hold one each.
Enough to prove a pipeline, nowhere near enough to train on. No forecast off
this will mean anything until the bridge has been running for a while.


## Out of scope

- Chain reading, RPC, ABI and contract config. The bridge owns all of it.
- Publishing. The neuron is a subscriber here.
- New Nostr event kinds. 34600 and 34601 are used as they are.
- `relay_ids.py`. Its uuid5 seeds are frozen and pinned by
  `tests/test_relay_ids.py`; changing them orphans every trained relay model.
