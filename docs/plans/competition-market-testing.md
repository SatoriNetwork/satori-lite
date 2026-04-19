# Competition Marketplace Manual Testing Plan

Status: **READY TO EXECUTE**
Scope: **Prediction competition market** (layer 2) and **approval-gated private streams** (Phase 5).
Prerequisite: Stream market testing completed (all scenarios verified 2026-04-13).

## 1. Goal

Test the prediction competition lifecycle and approval-gated private streams
end-to-end using the existing two-neuron simulation environment. Alice acts
as the competition host (and stream producer); Bob acts as the predictor
(and stream subscriber). Both roles exercise the full UI-to-relay-to-DB
pipeline.

## 2. Environment

Reuses the stream market simulation infrastructure — no new containers needed.

| Container | Role | Port | Source |
|---|---|---|---|
| `satori-sim-neuron` | Alice (host/producer) | 24601 | `/shared/satori-sim/` |
| `satori-sim-neuron-2` | Bob (predictor/subscriber) | 5000 | `/shared/satori-sim-2/` |
| `satori-relay-strfry-1` | Shared Nostr relay | 7777 (via nginx) | docker compose |

Both neuron source trees are now on `prediction-market` branch (commit `4354099`).
Both satorilib copies are on `stream-market` branch (commit `8086154`).

**Access URLs from dev container:**
- Alice: `http://docker-daemon:24601` (or outer host `http://localhost:24601`)
- Bob: `http://docker-daemon:5000` (or outer host `http://localhost:5000`)

**Important:** The containers must be restarted after the branch switch so
they pick up the new Python code. The bind mounts mean the files are already
on disk, but the running Python processes have the old modules cached.

## 3. Preconditions

Before starting competition testing, verify:

1. Both neurons are running and accessible (health check / dashboard loads)
2. Both have funded wallets (from stream market testing — no new funding needed)
3. Alice has at least one active published stream (e.g. `e2e-paid-ticker`)
4. The relay is up and both neurons can reach it
5. Both neurons show the **Competitions** page at `/competitions` with the
   new "Access Requests" tab visible

## 4. Test Scenarios

### Scenario 1 — Competition Announcement and Discovery

**Purpose:** Verify Phase 1 — Alice announces a competition, Bob discovers it.

**Alice (host):**
1. Navigate to `/competitions` -> "My Competitions" tab
2. Click "New Competition"
3. Select `e2e-paid-ticker` from the stream dropdown
4. Set: Total Pay per Obs = `200` sats, Predictors to Pay = `1`,
   Channels to Open = `1`, Scoring Module = `mae`, Horizon = `1`
5. Click "Announce"
6. **Verify:** Competition appears in "My Competitions" table with status "Active"

**Bob (predictor):**
1. Navigate to `/competitions` -> "Browse" tab
2. Click refresh
3. **Verify:** Alice's competition for `e2e-paid-ticker` appears in the list
4. **Verify:** Shows correct details: 200 sats/obs, mae scoring, 1 paid predictor
5. **Verify:** "Join" button is visible (not "Your competition" or "Joined")

### Scenario 2 — Join Competition and Submit Predictions

**Purpose:** Verify Phase 2 — Bob joins and predictions flow to Alice.

**Bob:**
1. Click "Join" on Alice's competition
2. **Verify:** Button changes to "Joined" badge
3. **Verify:** Bob's neuron is now subscribed to `e2e-paid-ticker` (check
   `/api/network/subscriptions`)

**Alice:**
1. Publish a new observation on `e2e-paid-ticker` (value = some number, e.g. `42.0`)
   via the dashboard publish UI or API: `POST /api/network/publish`
   ```json
   {"stream_name": "e2e-paid-ticker", "value": "42.0"}
   ```

**Bob:**
1. Bob's neuron should automatically predict the next observation
   (the prediction engine generates a prediction and submits it via encrypted DM)
2. **Verify:** Check Alice's DB for received predictions:
   `GET /api/competition/leaderboard?stream_name=e2e-paid-ticker&provider_pubkey=<alice_pubkey>`
   — should eventually show Bob's pubkey

**Note:** If the prediction engine doesn't auto-predict (it may need multiple
observations to build a model), we can submit a manual prediction via API:
```
POST /api/network/predict
{
  "stream_name": "e2e-paid-ticker",
  "stream_provider_pubkey": "<alice_nostr_pubkey>",
  "host_pubkey": "<alice_nostr_pubkey>",
  "seq_num": <next_seq>,
  "predicted_value": 43.0
}
```

### Scenario 3 — Scoring and Payment

**Purpose:** Verify Phase 3 — observation triggers scoring, host pays predictor.

**Precondition:** Bob has submitted at least one prediction for a future seq_num.

**Alice:**
1. Publish the observation that Bob predicted (the seq_num Bob predicted for)
2. **Verify:** Alice's `_competitionScoreAndPay` fires:
   - Check logs: `Competition: received prediction from <bob_pubkey>...`
   - Check `GET /api/competition/leaderboard` — Bob should have `total_sats > 0`
3. **Verify:** Alice opens a payment channel to Bob (or reuses existing one)
   - Check `/api/channels` on Alice — should show a sender channel to Bob's
     wallet pubkey
4. **Verify:** Payment recorded in `competition_payments` table:
   - `GET /api/competition/stats?stream_name=e2e-paid-ticker&provider_pubkey=<alice>&host_pubkey=<alice>`
   - Should show `scored_observations >= 1`, `total_paid_sats >= 200`

**Bob:**
1. **Verify:** Bob receives the channel payment (check `/api/channels` —
   should show a receiver channel from Alice)

### Scenario 4 — Leaderboard and Stats (Accountability)

**Purpose:** Verify Phase 4 — accountability UI shows correct data.

**Alice:**
1. Navigate to `/competitions` -> "My Competitions" tab
2. Click the leaderboard icon on the competition row
3. **Verify:** Leaderboard tab shows Bob ranked #1 with correct prediction count
   and total sats
4. **Verify:** Host stats bar shows:
   - `scored_observations` = number of observations scored
   - `total_paid_sats` = sum of all payouts
   - Follow-through % > 0

### Scenario 5 — Close Competition

**Purpose:** Verify competition lifecycle end.

**Alice:**
1. Navigate to "My Competitions" tab
2. Click "Close" on the competition
3. Confirm the dialog
4. **Verify:** Status changes to "Closed"

**Bob:**
1. Refresh the "Browse" tab
2. **Verify:** The competition no longer appears (or appears as inactive if
   `?active=0` is used)

### Scenario 6 — Multiple Predictions, Late Arrival Recovery

**Purpose:** Verify late-arrival scoring path.

**Alice:**
1. Create a new competition on the same or different stream
2. Publish observation for seq_num N

**Bob:**
1. Submit a prediction for seq_num N **after** Alice has already published
   the observation (late arrival)
2. **Verify:** Alice's `_competitionScoreLateArrival` fires and scores
   immediately
3. **Verify:** Bob appears on the leaderboard with payment for seq_num N

### Scenario 7 — Access Request Flow (Phase 5)

**Purpose:** Verify approval-gated private streams end-to-end.

**Alice (producer):**
1. Navigate to `/competitions` -> "Access Requests" tab
2. **Verify:** The tab loads with three sections: Pending Requests, Approved
   Subscribers, Request Access form
3. Verify no pending requests initially

**Bob (subscriber):**
1. Navigate to `/competitions` -> "Access Requests" tab
2. In the "Request Access to a Private Stream" form:
   - Stream name: `e2e-paid-ticker` (or a new stream Alice creates)
   - Producer pubkey: Alice's nostr pubkey (from Alice's settings/status API)
   - Message: `Testing access request`
3. Click "Request Access"
4. **Verify:** Success message appears

**Alice:**
1. Refresh the "Access Requests" tab (or check the pending badge)
2. **Verify:** Bob's request appears in the Pending Requests table
3. **Verify:** Shows stream name, Bob's pubkey (truncated), message, timestamp
4. Click "Approve"
5. **Verify:** Request disappears from pending list
6. Select the stream in the "Approved Subscribers" dropdown
7. **Verify:** Bob appears in the approved subscribers list

### Scenario 8 — Revoke Access

**Purpose:** Verify revocation removes subscriber.

**Alice:**
1. In the "Approved Subscribers" section, click "Revoke" on Bob
2. Confirm the dialog
3. **Verify:** Bob disappears from the approved list
4. **Verify:** The access_requests table shows status = 'revoked' for Bob
   (check via `GET /api/access/requests?stream_name=<name>&status=revoked`)

### Scenario 9 — Reject Access Request

**Purpose:** Verify rejection flow.

**Bob:**
1. Submit another access request to Alice's stream

**Alice:**
1. See the pending request
2. Click "Reject"
3. **Verify:** Request disappears from pending list
4. **Verify:** Status is 'rejected' in the API response

**Bob:**
1. Can re-request (submit again)
2. **Verify:** Alice sees a new pending request (re-request resets to pending)

## 5. Verification Methods

Each scenario can be verified through three channels:

1. **Browser UI** — visual confirmation via Playwright MCP tools
   (`browser_navigate`, `browser_snapshot`, `browser_click`)
2. **API** — direct HTTP calls to the neuron REST endpoints
3. **Database** — `network.db` SQLite queries inside the container
   (via `docker exec`)

The preferred approach is UI-first (proves the full stack) with API
spot-checks for data that isn't surfaced in the UI.

## 6. Execution Order

1. Restart both neuron containers (pick up new code from branch switch)
2. Verify preconditions (Scenario 0 — health check)
3. Run Scenarios 1-5 in order (competition lifecycle — depends on prior state)
4. Run Scenario 6 (late arrival — can use same or new competition)
5. Run Scenarios 7-9 in order (access gate lifecycle — independent of competitions)

## 7. Expected Issues

- **Prediction engine may not auto-predict.** If Bob's neuron needs a history
  of observations before the engine fires, we'll use the manual predict API
  endpoint to submit predictions directly.
- **Channel funding takes block confirmations.** If Alice's competition
  payment triggers a new channel open to Bob, expect 1-2 minute delay for
  the funding tx to confirm. Poll `/api/channels` on both sides.
- **Container restart clears in-memory state.** After restart, the prediction
  listeners, access request listeners, and subscriber lists need to
  re-initialize. The DB state persists, but in-memory queues start empty.
  Verify listeners are running by checking logs.

## 8. Tracking

Results will be documented in `docs/plans/competition-market-test-status.md`
following the same format as `stream-market-test-status.md`.
