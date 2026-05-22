# Payment Channel & Paid Stream — Fix Plan

Issues found while debugging the paid-stream + payment-channel flow between
two neurons (provider + paying subscriber). Grouped by **root cause** — most
items share an underlying bug, so one fix often closes several symptoms.

Each group below has a one-paragraph explanation and the files affected.
Implementation order is at the bottom.

---

## Already landed

- `p`-tag check in `_handle_observation_event` (drops cross-subscriber decrypt
  spam).
- UTXO-mismatch rejection plus a startup tombstone scan so commitments whose
  funding UTXO was already spent are cleaned up on reconnect.
- DB writes in `claimChannel` reordered to occur before any Nostr publish, so
  a crash between the two can't leave state divergent.
- 3-second retry for the commitment-before-channel-open arrival race.
- `grant_to_seq` initialised before the per-client loop (race with multi-relay
  grants).

---

## A. Settlement publish must be verified — **critical**

`claimChannel` treats "published settlement to 0 relays" as a warning and
still clears the pending commitment. The sender never learns the UTXO moved,
keeps building commitments against a spent output, and state diverges
silently. If `published_count == 0` after the publish loop, do **not** clear
`pending_commitment` and raise so the operator can retry.

**Files:** `neuron-lite/start.py` (`claimChannel`, settlement publish block).

---

## B. Pin `price_per_obs` to each commitment

Price is read live from the publications table on both sides. If the provider
edits the price between the subscriber building a commitment and the receiver
granting access, the subscriber either overpays or is granted zero new
observations (`pay_amount // new_price == 0`). Carry the agreed price on the
commitment itself:

1. Add `price_per_obs` to `ChannelCommitment`.
2. Sender stamps it from the current subscription row when building.
3. Receiver uses `commitment.price_per_obs` in `_grantChannelAccess` rather
   than the live publication row.

**Files:** `satorilib/satori_nostr/models.py`, `neuron-lite/start.py`
(`_channelPayNow`, `_grantChannelAccess`).

---

## C. Key subscriber state on the channel, not the Nostr pubkey

Provider's `last_paid_seq` lives in memory keyed by subscriber Nostr pubkey
and resets on provider restart, on the subscriber rotating Nostr keys, and on
any disconnect. This opens two holes: (a) a subscriber can get an unlimited
stream of "free sample" observations by cycling Nostr keys, and (b) on
reconnect the provider has no record of what the subscriber already paid for,
leading to deadlocks where the next seq is neither `paid` nor `free`.

Persist `last_paid_seq` in the DB keyed by `(stream_name, channel_p2sh)` —
the channel is the identity anchor across Nostr key rotations. Provider loads
it at startup and on subscription re-announce.

**Files:** `satorilib/satori_nostr/client.py` (`_subscribers` bookkeeping),
`neuron-lite/satorineuron/network_db.py` (new table / columns),
`neuron-lite/start.py` (load/save hooks).

---

## D. `funding_txid` change is the authoritative reset signal

Several monotonicity and staleness checks compare timestamps on incoming
commitment / channel-open events. Relay delivery latency, clock skew, and
zero-coerced missing timestamps all cause legitimate state transitions (most
importantly refunds) to be rejected. Use `funding_txid` as the source of
truth: when an incoming event references a different UTXO than the one in
our DB, treat it as a fresh state transition and bypass the monotonicity /
timestamp checks. Keep those checks for same-UTXO replays only. Reject
events with missing or zero timestamps instead of coercing.

**Files:** `neuron-lite/start.py` (`_channelProcessCommitment`,
`_channelHandleOpen`).

---

## E. Block channel operations until the wallet is ready

`WalletManager.connect()` returns False on startup; the wallet recovers
moments later, but `openChannel` / `refundChannel` / `_channelPayNow` that
fire in that window die with swallowed warnings and leave the channel in a
half-baked state that survives until a manual restart. Gate the channel ops
behind a `wallet.ready` flag / future; surface a real error on timeout and
schedule one retry once the wallet is up. Demote the startup warning once a
retry succeeds.

**Files:** `neuron-lite/satorineuron/init/wallet.py`,
`neuron-lite/start.py` (channel op entrypoints).

---

## F. Payments should fire on state, not on events

Today, `_channelPayForObservation` is gated by `is_new` — a payment only
fires when the incoming observation was not already in the subscriber's DB.
Combined with the cooldown timer, this over-pays when observations arrive in
bursts and under-pays when observations are stale or replayed. Replace the
trigger with a single state-based rule: fire a payment iff
`last_paid_seq < publisher_last_seq` for an active paid subscription.
Cooldown stays as a rate-limiter inside the trigger, not the entrance gate.

**Files:** `neuron-lite/start.py` (`_networkProcessObservation`,
`_channelPayForObservation`).

---

## G. Unsubscribe lifecycle

Unsubscribe is a local soft-delete with no signal to the provider. The
provider keeps encrypting observations for a subscriber who won't decrypt,
and the subscriber's channel funds stay locked for the CSV window (90 days
by default; see L). The library already exposes `unsubscribe_datastream`
with an `action=unsubscribe` tag; wire it up on the UI route, and have the
provider's `_handle_subscription_event` honor the tag: remove the subscriber
from `_subscribers`, auto-claim the pending commitment, and tombstone the
channel on the relay.

**Files:** `web/routes.py` (`api_network_unsubscribe`),
`satorilib/satori_nostr/client.py` (`_handle_subscription_event`),
`neuron-lite/start.py` (auto-claim on unsubscribe).

---

## H. Serialize commitment builds per channel

Two observations arriving within the cooldown window can both read
`remainder_sats = X` before either writes back, producing two commitments
that reference the same prior state and advance the channel by only one
increment. Wrap the read-build-write in `_channelPayNow` with a per-channel
`asyncio.Lock`.

**Files:** `neuron-lite/start.py` (`_channelPayNow`).

---

## I. Require `stream_name` on every commitment

A commitment with an empty `stream_name` causes `_grantChannelAccess` to
loop over every paid publication the provider owns and grant each one
`total_paid_sats / its_price` observations — a cross-stream over-grant. Treat
a missing or empty `commitment.stream_name` as an invalid commitment and
reject at validation time; only grant to the named stream.

**Files:** `neuron-lite/start.py` (`_channelProcessCommitment`,
`_grantChannelAccess`).

---

## J. Idempotent retry for Mundo PATH C

If Mundo rejects a claim mid-flow, the next attempt refetches fee params
and may select different input UTXOs, invalidating the signature from the
earlier attempt. Cache the built unsigned transaction and fee params on the
first attempt and reuse them on retry. Ask Mundo to return a broadcast
receipt / txid so the receiver can check on-chain status before re-attempting.

**Files:** `neuron-lite/start.py` (`_claimChannelViaMundo`,
`_reclaimChannelViaMundo`).

---

## K. Dynamic channel funding by observation economics

`_channelFundSats()` returns a hardcoded 1,000,000 sats regardless of stream
price. For a `price_per_obs=1000` stream that covers 1000 observations;
for `price_per_obs=100000` it covers 10 and causes a constant refund churn,
paying mining fees for what could be one larger deposit. Compute the deposit
from the subscription economics:

```
fund_sats = clamp(min_fund, price_per_obs * observations_per_refill, max_fund)
```

where `observations_per_refill` (config, default ~500) is how many obs we
prepay before a refund is needed. `_channelPayNow` already has the
subscription in scope at the auto-open site.

**Files:** `neuron-lite/start.py` (`_channelFundSats`, `_channelPayNow`).

---

## L. Shorten the default channel timeout

`channel_timeout_minutes` defaults to 129,600 (90 days). Drop the default to
10,080 (7 days) so subscribers' funds aren't idle for a quarter of a year in
the normal failure case. The sender still has a generous reclaim window.

**Files:** `neuron-lite/start.py` (`_channelTimeoutMinutes`).

---

## M. Don't gate paid-subscription listeners on relay freshness

**Highest-impact live bug.** On subscriber restart, `_networkReconcile` asks
the relay for the latest kind 34601 event for the stream. For a paid stream
where the provider has no in-memory subscribers, `publish_observation` sends
zero events, so the relay's latest-event timestamp stays frozen at whenever
there was last a paying subscriber. The reconcile's freshness check sees a
stale timestamp, marks the stream `stale everywhere, recheck in 24h`,
disconnects, and never starts a listener — which means the subscriber never
announces, which means the provider never adds them to `_subscribers`,
which means no more events are published, which means the relay stays stale.
24-hour lockout. The active channel sits untouched.

Fix: for subscriptions with `price_per_obs > 0 and active = 1`, skip the
`_networkCheckFreshness` gate in reconcile. We already have a signed channel
and a declared intent; freshness is circular for paid streams. Reserve the
check for discovering new streams.

Optional provider-side complement: emit a plaintext heartbeat (kind 34601
with no `p` tag) each cadence so discovery-time freshness reflects publisher
activity. Not required once the subscriber-side bypass lands.

**Files:** `neuron-lite/start.py` (`_networkReconcile` hunt loop).

---

## N. Align `discover_datastreams` limit

`_networkReconcile` at the hunt site calls `client.discover_datastreams()`
with the default `limit=100`; the two sibling call sites pass
`limit=1000`. Current relay serves 460 streams — any subscriber whose
target isn't in the first 100 returned is falsely told "not on this relay"
and falls through to `mark_stale` (compounds M). Either raise the limit or,
better, fetch by `#d=<stream_name>` directly since we already know the names
we want.

**Files:** `neuron-lite/start.py` (`_networkReconcile`), possibly
`satorilib/satori_nostr/client.py` if switching to a by-name fetch.

---

## O. Library-side `p`-tag filter for kind 34604

Stale third-party commitments on the relay (abandoned channels we don't own
and can't tombstone — kind 34604 is parameterized-replaceable only by the
original author) are pushed to every client on every reconnect. Each one
triggers a DB lookup, a 3-second race-retry, and a misleading
`WARNING - Channel: commitment for unknown channel <p2sh>` log line. For N
stale events across M relays this is 3·N·M seconds of wasted sync time per
reconnect, on top of the noise.

Non-tombstone commitments are point-to-point: `publish_commitment` sets a `p`
tag to the intended receiver's Nostr pubkey. Mirror the fix already landed
for `_handle_observation_event`: in `_handle_commitment_event`, drop any
event whose `p` tag doesn't match our pubkey. Tombstones (empty content, no
`p`) still pass through.

**Files:** `satorilib/satori_nostr/client.py` (`_handle_commitment_event`).

---

## Implementation order

| Step | Fix | Why |
|---|---|---|
| 1 | **A** | Critical; tiny change; prevents the silent-divergence class of bugs hit twice in testing |
| 2 | **M** | Live deadlock today — unblocks end-to-end testing of everything else |
| 3 | **N** | One-line companion to M; together they fix subscriber bootstrap reliability |
| 4 | **D** | Makes refund flow robust; unblocks testing of E/G/H |
| 5 | **E** | Unblocks automated channel open/refund/claim |
| 6 | **F** | Removes both over-pay and deadlock paths in one rule change |
| 7 | **C** | Closes the free-sample exploit and the reconnect deadlocks |
| 8 | **B** | Price pinning — once state is stable, pin the economics |
| 9 | **G** | Unsubscribe lifecycle — user-visible correctness |
| 10 | **O** | Cleanup — removes log spam + wasted sync time |
| 11 | **H, I, J, K, L** | Hardening and config |

## Anti-patterns to avoid

- Ad-hoc timestamp tolerances. Fix D removes the need.
- New reconciliation timers. Fix F is "trigger on state, not on time".
- A separate `claimed` flag on channels. UTXO diff is already the source of
  truth (used by the landed stale-tombstone fix).
- Reading `price_per_obs` off a publication row during a grant. Fix B pins it.
