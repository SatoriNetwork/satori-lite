# Network System Concerns

Outstanding items from architecture review. Fix as we go.

## 3. `_networkSubscribed` is in-memory only
If the neuron restarts, this set is empty, so on first reconcile pass it
re-announces subscriptions (kind 34602 events) to every relay for every
stream. Not harmful but noisy — publishes duplicate subscription announcements.

## 4. ~~No actual data flow~~ (FIXED)
Observation listeners run per-relay via `_networkListen()`. Each reads
from `client.observations()` and saves to the local SQLite DB. Listeners
start when the hunt finds active streams and are cancelled on disconnect.

## 5. ~~Central is a single point of failure for relay discovery~~ (FIXED)
Both the reconciliation loop and on-demand discovery now fall back to
relay URLs stored in the subscriptions DB when `server.getRelays()` fails.

## 6. ~~Duplicate freshness checks~~ (FIXED)
Subscribed streams now check staleness via local observation DB
(`is_locally_stale`). Only the relay hunt phase queries relays remotely.

## 7. No auth on `GET /api/v1/peer/relays`
The central endpoint has no authentication — anyone can enumerate all relay
URLs. Might be intentional (relays are public infrastructure) but worth a
conscious decision.
