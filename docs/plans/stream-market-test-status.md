# Stream Market Test Status

Last updated: 2026-04-13 (session 4)

## Environment

- Two Docker neurons: Alice (publisher, port 24601) and Bob (subscriber)
- Local Nostr relay: strfry + nginx reverse proxy at `ws://nginx/`
- Isolated source trees: `/shared/satori-sim/` (Alice), `/shared/satori-sim-2/` (Bob)
- Real Evrmore mainnet wallets funded with EVR + SATORI
- Docker-in-Docker with `DOCKER_HOST=tcp://docker-daemon:2376`

## Verified Working

### Stream Discovery and Subscription
- [x] Alice publishes `KIND_DATASTREAM_ANNOUNCE` (34600) for `e2e-paid-ticker` (price_per_obs=100 sats)
- [x] Bob discovers the stream on the shared Nostr relay
- [x] Bob subscribes to the stream (recorded in his networkDB)

### Payment Channel Opening
- [x] Bob calls `openChannel` — funds 2-of-2 P2SH multisig with CSV timeout
- [x] `KIND_CHANNEL_OPEN` (34605) published to relay
- [x] Alice receives channel open event, registers channel in her DB

### Commitment Publishing and Delivery
- [x] Bob's `KIND_CHANNEL_COMMITMENT` (34604) events accepted by relay (32-byte Nostr pubkey in `p` tag)
- [x] Alice receives commitments (no silent drop from broken filter)
- [x] Alice stores `pending_commitment` in her DB

### Auto-Micropayment
- [x] `_channelPayForObservation` fires automatically per received observation
- [x] Each payment publishes a new commitment with correct cumulative semantics
- [x] `pay_amount_sats` is the per-round delta; partial tx carries cumulative total

### Per-Payment Access Gating (core value proposition)
- [x] Bob paid -> received observations (seq 3, 4, 5)
- [x] Channel drained to remainder=0 -> auto-pay failed -> seq 6 (value 77.77) NOT delivered
- [x] Confirms: no pay = no data

## Bugs Found and Fixed (all pushed to canonical stream-market branch)

| # | Bug | Root Cause | Fix | Repo |
|---|-----|-----------|-----|------|
| 1 | Relay rejects commitment events (`unexpected size for fixed-size tag: p`) | `publish_commitment` used 33-byte EVR wallet pubkey for Nostr `p` tag (needs 32-byte x-only) | Added `receiver_nostr_pubkey` parameter | satorilib |
| 2 | Alice silently drops all commitments | `_handle_commitment_event` compared `commitment.receiver_pubkey` (wallet) to `self.pubkey()` (Nostr) — always mismatched | Removed broken filter; consumer filters by p2sh_address DB lookup | satorilib |
| 3 | `sendChannelPayment` passes raw pubkey as address | `toAddress=channel['receiver_pubkey']` but `_compileClaimOnP2SHMultiSigStart` expects P2PKH address string | `EvrmoreWallet.generateAddress(channel['receiver_pubkey'])` | neuron |
| 4 | `receiver_nostr_pubkey` not persisted | `openChannel` accepted it but never saved to DB; `sendChannelPayment` couldn't read it | Added column + migration + `save_channel` kwarg | neuron |
| 5 | Channels deleted when drained | `delete_channel` called in 4 places; channels are supposed to be persistent and refunded | Replaced with `reset_channel_funding`/`update_channel_remainder(0)`; added `refundChannel` method | neuron |
| 6 | ElectrumX connection: `connected()` vs `ensureConnected()` | Wallet methods bailed if not already connected instead of trying to reconnect | Changed 8 call sites in `wallet.py` | satorilib (pushed by someone else) |
| 7 | `refundChannel` fails: "not enough satori to send" | `wallet.divisibility` is 0 after restart because `getStats()` never ran; `roundSatsDownToDivisibility(10000, 0)` → 0 | Added `await asyncio.to_thread(self.wallet.getReadyToSend)` before building tx | neuron |
| 8 | `claimChannel` crashes: `Object is immutable` | `CMutableTransaction.deserialize()` creates immutable `CTxIn`/`CTxOut` sub-objects; `_compileClaimOnP2SHMultiSigEnd` can't set `scriptSig` | Convert vin/vout to `CMutableTxIn`/`CMutableTxOut` after deserialize | neuron |
| 9 | `sendChannelPayment` builds partial tx paying receiver 0 | `wallet.divisibility` defaults to 0 after restart; `roundSatsDownToDivisibility(300, 0)` → 0; receiver output omitted, ALL SATORI goes to P2SH change | Changed default `self.divisibility = 8` in wallet.py (SATORI is div=8 on-chain); also added `getReadyToSend` guard as belt-and-suspenders | satorilib + neuron |
| 10 | PATH C Mundo: `claim mismatch, _verifyClaimAddress` | `_claimChannelViaMundo` put SATORI fee BEFORE change; Mundo checks `vout[-2]` for fee address but found Alice's change address | Swapped output order: SATORI change → SATORI fee → EVR change (matches `satoriOnlyPartialSimple` convention) | neuron |

## Not Yet Tested

### Channel Claiming (receiver broadcasts)
- [x] Alice signs the partial tx, broadcasts it, UTXO gets spent on-chain (txid: 6b01c97b...)
- [ ] `claimChannel` PATH A (Alice has EVR for fees) — not tested (partial tx has fee=0, so always goes to PATH B)
- [x] `claimChannel` PATH B (Alice has EVR, adds EVR input for fee) — verified working
- [x] `claimChannel` PATH C (Mundo pays the fee) — verified working (txid: 4b57bad5...). Output order fix: change→fee→EVR change so vout[-2]=fee matches `_verifyClaimAddress`. Second claim blocked by stale Mundo EVR UTXO (server-side cache issue).

### Post-Claim Channel Reset Cycle
- [x] After Alice claims, Bob's channel updates with new funding UTXO (manually; settlement event not yet tested)
- [x] Bob's cumulative tracking resets to 0 (remainder=locked=10000)
- [x] Bob can send new micropayments on the refreshed channel (remainder 10000→9900 on claimed UTXO 6b01c97b...:0)

### Channel Refund
- [x] Auto-trigger: when channel drained, `_channelPayForObservation` calls `refundChannel` — verified (txid: ca42e825...)
- [x] `refundChannel` sends new SATORI to existing P2SH via `producePaymentChannelFromScript` (txid: 110c8a74...)
- [x] DB updated with new `funding_txid`/`funding_vout`/`locked_sats`/`remainder_sats`
- [x] Payments resume on the refunded channel — auto-payment fires on new observations, remainder decrements correctly

### Channel Reclaim by Sender
- [x] `reclaimChannel` after CSV timeout — sender gets funds back (txid: 90f46c93..., 9400 SATORI to Bob)
- [x] `remainder_sats` zeroed to prevent use of spent UTXO
- [x] Channel row persists (never deleted)

### Tombstone / Settlement Events
- [x] `KIND_CHANNEL_SETTLED` (34606) delivered from Alice to Bob after claim
- [x] Bob processes settlement — updates channel with new UTXO (txid, vout, locked_sats, remainder all correct)
- [x] Tombstone fallback when settlement not received — zeros remainder (verified: tombstone published, Bob zeroed remainder from 9700→0)

### Partial TX Validity
- [x] Partial transactions built by `sendChannelPayment` are valid and broadcastable (proven by claim txids 6b01c97b... and 2f5a5330...)
- [x] Post-div-fix partial txs correctly include receiver output with cumulative SATORI amount

### Encrypted Observations
- [x] Paid stream observations encrypted via NIP-04 per subscriber (Alice encrypts for Bob's nostr pubkey)
- [x] Bob can decrypt observations addressed to him (nip04_decrypt with secret_hex from nostr.yaml)
- [x] Random keys cannot decrypt (confirmed)

### Multi-Relay
- [x] Events published to one relay do NOT appear on the other (relays don't sync)
- [x] `send_event_builder` pushes to all connected relays when client has multiple
- [x] Neuron loops over `_networkClients.values()` for commitments/settlements — correct pattern
- [x] Each relay maintains independent event set; subscriber sees union via per-relay listeners

### Free Stream (Scenario B from test plan)
- [x] Alice publishes free stream (price_per_obs=0) — `e2e-free-ticker` announced and observed
- [x] Bob subscribes and receives observations without payment (values 123.45, 456.78 delivered, no commitment published)

### Payment Rate Limiting (anti-gaming)
- [x] Normal cadence: observations at expected interval, each gets paid immediately (seq=21 after 82s gap → immediate)
- [ ] Slight variation: observation arrives within cadence but outside cooldown (cadence/2) — paid immediately
- [x] Flood: seller sends many observations within cooldown — only 1 immediate + 1 deferred payment per cadence (4 obs in 2s → 2 payments)
- [x] Deferred payment fires at cooldown end — seller receives it, relationship continues (fired at ~15s after first)
- [x] Flood then silence: deferred fires, then seller resumes normal cadence — payments resume normally (seq=21 → immediate)
- [x] No cadence (irregular stream): every observation paid immediately, no rate limit (verified in earlier sessions with cadence=0)
- [x] Verify deferred payments don't stack (only one pending per subscription) (3 obs during cooldown → only 1 deferred fired)

## Test Scenarios (from e2e-testing.md)

| Scenario | Description | Status |
|----------|-------------|--------|
| A — First-run funding | Manual wallet setup | Done (wallets funded) |
| B — Free stream discovery | Smoke test, no payment | Verified (e2e-free-ticker) |
| C — Priced stream, no payment | Access denied verification | Partially verified (seq 6 gated) |
| D — Priced stream, channel + payment | Full payment flow | Verified (seq 3-5 delivered after payment) |
| E — Channel exhaustion | Drain and verify gating | Verified (drained to 0, seq 6 blocked) |
| F — Reclaim after timeout | CSV timeout reclaim | Verified (txid: 90f46c93...) |
| G — Payment rate limiting | Anti-gaming: flood obs, verify max 2 payments/cadence | Verified (4 obs → 2 payments) |

## Verified Working (session 2)

### Channel Refund + Payment Resumption
- [x] `refundChannel` broadcasts on-chain tx sending 10,000 SATORI sats to existing P2SH (txid: 110c8a74...)
- [x] DB updated with new funding_txid, locked_sats=10000, remainder_sats=10000
- [x] `sendChannelPayment` builds valid partial tx from refunded UTXO, sender signs
- [x] Auto-payment fires on observation receipt: published commitments, remainder decrements 100 sats per obs
- [x] Multiple consecutive payments work (10000→9900→9800→9700)

### Live Observation Delivery
- [x] Production relay delivers live events correctly via `handle_notifications`
- [x] ~500 stored events (from ~494 streams across 22 authors) processed on connect, then live events flow
- [x] Only `e2e-paid-ticker` observations trigger payment (subscription filter works)

### Channel Claim (PATH B — receiver adds EVR for fee)
- [x] Claim 1 (before div fix): partial tx had 0 receiver output due to div=0 bug (txid: 6b01c97b..., 168 confs)
- [x] Claim 2 (after div=8 default fix): Alice receives 600 SATORI sats on-chain (txid: 2f5a5330..., confirmed)
  - vout[0]: 600 SATORI to Alice (P2PKH) — correct cumulative of 6 payments x 100
  - vout[1]: 9400 SATORI to P2SH (channel change for next round)
  - vout[2]: EVR change to Alice
- [x] Post-claim payment on new UTXO works (Bob: remainder 9400→9300, 2 outputs)
- [x] Full cycle verified: pay → accumulate → claim → reset → pay again

## Verified Working (session 3)

### PATH C: Claim via Mundo (receiver has no EVR)
- [x] Bug #10 fixed: output order swapped so `vout[-2]`=Mundo fee, matching `_verifyClaimAddress` expectation
- [x] Mundo accepts tx (returns 200, signs its EVR input)
- [x] First claim broadcast on-chain (txid: 4b57bad5..., 9400 SATORI to P2SH, 1M fee to Mundo, 1M change to Alice)
- [x] Second claim blocked by stale Mundo EVR UTXO cache (server-side issue, not a code bug)

### Divisibility Fix Verified in Running Neuron
- [x] After restart with div=8 default, `sendChannelPayment` correctly builds 2-output partial tx (receiver + P2SH change)
- [x] Confirmed: 100 sats to receiver, 9300 sats to P2SH change (was previously all 9400 to P2SH with div=0)

### Channel Reclaim by Sender (CSV timeout)
- [x] `reclaimChannel` PATH A: Bob has EVR, builds CSV single-sig tx (OP_FALSE branch), broadcasts (txid: 90f46c93...)
- [x] CSV timeout (10 min / ~17 min actual) expired with 29+ confirmations
- [x] 9400 SATORI reclaimed to Bob's address + EVR change
- [x] DB: remainder zeroed, channel row persists
- [x] Channel successfully re-funded via `producePaymentChannelFromScript` (txid: e34073b0...)

### Free Stream
- [x] Alice publishes `e2e-free-ticker` (price_per_obs=0) — stream announce + observations
- [x] Bob subscribes and receives plaintext observations (values 123.45, 456.78)
- [x] No payment/commitment triggered for free stream (`_channelPayForObservation` returns early)

### Payment Rate Limiting
- [x] Cadence=30s, cooldown=15s set on `e2e-paid-ticker` subscription
- [x] Flood test: 4 observations within 2 seconds → only 2 payments (1 immediate + 1 deferred at cooldown end)
- [x] Deferred fires at ~15s after first payment (expected: 15s, actual: 15.3s)
- [x] Deferred payments don't stack: 3 obs during cooldown → only 1 deferred
- [x] After cooldown expires: next observation paid immediately (seq=21 after 82s gap → immediate, 9800→9700)

## Verified Working (session 4)

### Tombstone Fallback (no settlement)
- [x] Alice claims channel (PATH B, txid: a85366f6...) and publishes ONLY tombstone (empty KIND_CHANNEL_COMMITMENT with `d=p2sh_address`, `action=claimed` tag)
- [x] Settlement intentionally withheld
- [x] Bob receives tombstone via `_channelTombstoneListen` → `_channelHandleTombstone` fires
- [x] Bob's remainder zeroed from 9700→0, locked_sats preserved at 10000
- [x] Channel row persists (never deleted)

### Auto-Refund Trigger (channel drained → auto-refund on next observation)
- [x] After tombstone zeroed remainder, Alice publishes observation (seq=25, value=555.55)
- [x] Bob receives observation, `_channelPayNow` detects `remainder=0 < price=100`
- [x] `refundChannel` auto-fires: broadcasts 10000 SATORI sats to existing P2SH (txid: ca42e825...)
- [x] Immediately after refund, commitment published: pay=100, remainder=9900
- [x] Full cycle: tombstone → zero → observation → auto-refund → auto-pay (no manual intervention)

## Known Issues

### Manual test scripts must use correct event format
- `DatastreamObservation.from_json` expects `stream_name`, `timestamp`, `value`, `seq_num`
- Earlier manual publishes used wrong keys (`stream`, `ts`, `seq`) — events were silently dropped as decryption failures
- Fix: always publish with correct DatastreamObservation format

## Remaining

1. **`claimChannel` PATH A** — not tested (partial tx has fee=0, always goes to PATH B). Low priority.
2. **Slight cadence variation** — observation within cadence but outside cooldown (cadence/2). Not tested. Low priority.
