# Satori Prediction Market

## Vision (Verbatim)

> Maybe a company has a data stream or many data streams that they want predicted — we'll just pretend they have one. They have one data stream they want predicted, so they post it up for free, anybody can see the data. Then they host a competition and the competition says: I am going to pay the best X amount of predictors X amount of tokens for every observation.
>
> The tricky thing about this is they get to decide how to score the predictors. They might say: I'm just going to go with a typical scoring, and we'll have some of these baked in. But maybe they say I just want straight up accuracy on the next timestamp, the next observation — if you predict that the best you're at the top of the list. But the interesting thing about predicting the future is you may want to weight predictions that are better at foreseeing the future in the long run. You may want to weight them more or give them more payment. You may not want to just use straight up accuracy as your scoring metric. So the competition host gets to choose how they will score the predictors.
>
> They do score them and they keep their promise that they'll pay the best three predictors, or whatever number they want, however much money they promise, every observation. That way they can broadcast these payments openly — I don't know if the payment channels are open but I bet they are — they can broadcast these payments openly and people can then add the payments up to make sure that the host is doing what they promised and paying the amounts that they promised. This is important because if the host promises to pay a hundred Satori every day and everybody would want to predict their data stream, but then if they lie to everybody and say "well you weren't the top" they get all the predictions for free because they don't pay anybody. So we have to know that they're making these payment commitments and we have to be able to add them up, even though the community can't broadcast those payments because only the person that it's for can do so.
>
> This also means that the host can put a limit on how many people are allowed to predict. For instance they could say: I'm only going to pay the top three out of five predictors because I don't want to make a channel for every single predictor who gives me some random prediction. Opening a channel costs tokens, so I'm not going to open channels for thousands of predictors — you've got to be in the top five for like a month or something in order for me to open a channel and start paying you. So they can make rules like that, and it doesn't have to be public. I think it should be, but that's part of the scoring mechanism and they can do whatever they want with scoring, but they do have to pay out what they promised to pay or else everybody can see that they're not paying and nobody will want to predict for them.
>
> So the idea is that they post up a data stream — or they don't have to post it, maybe somebody else posts it and they just want to host the competition on the data stream. I think that means we're going to need a new Nostr relay type or kind so that we can announce a competition, and I think we probably want to be able to un-announce a competition because if the competition ends we want to tell everybody it's done. So I can envision those two updates to the Nostr API.
>
> After that I don't think there's too much API work that has to be done. It's kind of like the predictors subscribe to the data just like they normally would, they start predicting it just like they normally would. But here's one more thing about hosting a competition: by default, and perhaps this is the only option, the predictions are private. What I mean by that is the predictions are sent to the host of the competition directly as a DM — they are not broadcast publicly. So the predictors would send the predictions directly to the competition host, the host would score them according to their own metric and pay out according to their own metric, and the relationship would continue as long as the host is paying.
>
> It's courteous for the host to announce that they have stopped the competition when they do decide to stop, but everybody can see the amount of money in the channels and if there's nothing left everybody can know this competition is dead.
>
> During the competition the host will pay out the tokens on the channels — it's not very much because it's probably just a little bit, but that's all in the announcement: how much is paid. The host can re-announce for the same data stream and it's just a replacement of the competition — everybody knows okay, that means they've changed the price that they're willing to pay, or whatever. So the host should be able to re-announce the competition.
>
> That reminds me of just announcing the data stream and its cost — that's how that should work too. You should be able to announce your data stream again if you're charging for your data stream. So I don't know if that's actually baked into what we've built already but that's probably important if it's not.
>
> Then that's basically it with this feature. People can come onto the Satori network and request that the network predict their private data. They can't keep that data private, but what they can do is not let everybody know the predictions — the predictions are meant to be private, just for them.
>
> As a stretch goal: I think hosts should be allowed to authorize predictors, not only because they don't want to have to make a bunch of data stream channels, but also because perhaps there are predictors they trust and they want to send them encrypted data. They want the data stream to be encrypted, and therefore they can authorize particular predictors and give them the prediction key. They'll probably change that key on occasion so they have to broadcast it out again. That seems like an extra layer of features on top of this competition framework, but I think that is also a valuable thing to do in case a company or something wants some kind of pseudo-privatization of their data so that it's not just broadcast publicly for everybody to see.
>
> Over time we could credentialize certain neurons, certain predictors — we know who they are, they have a business relationship, they always keep private information private. Being able to choose your predictors and give them encrypted data would be a valuable, trustworthy thing to do.

---

## Overview

The prediction market is the second layer of the Satori data stream marketplace. The first layer (already built) lets producers charge for data. This layer lets **hosts** pay for predictions of data — essentially a structured, open, accountable prediction competition running over the same Nostr + payment channel infrastructure.

---

## Roles

| Role | Description |
|------|-------------|
| **Host** | Posts a data stream (or references an existing one), announces a competition, scores predictors, and pays winners via channels |
| **Predictor** | Subscribes to the data stream, sends predictions privately to the host as encrypted DMs, earns SATORI per observation if they rank |
| **Observer** | Anyone on the network — can see competition announcements, verify channel payment activity, and assess whether a host is trustworthy |

---

## Nostr Event Kinds

Two new kinds are needed:

| Kind | Name | Type | Description |
|------|------|------|-------------|
| 34607 | `KIND_COMPETITION_ANNOUNCE` | Parameterized replaceable (`d=stream_name:host_pubkey`) | Host announces or updates a competition. Re-publishing replaces the previous announcement. |
| 34608 | `KIND_PREDICTION` | Encrypted DM | Predictor sends a prediction directly to the host. Not broadcast publicly. |

`KIND_COMPETITION_ANNOUNCE` being parameterized replaceable means re-announcing is free — it simply replaces the old event on the relay. A host closes a competition by publishing a tombstone (empty content) for the same `d` tag, or by draining their payment channels (which observers can see).

### KIND_COMPETITION_ANNOUNCE Content

```json
{
  "stream_name": "btc-price-usd",
  "stream_provider_pubkey": "hex...",
  "host_pubkey": "hex...",
  "pay_per_obs_sats": 100,
  "paid_predictors": 3,
  "competing_predictors": 5,
  "scoring_metric": "mae",
  "scoring_params": {},
  "horizon": 1,
  "active": true,
  "timestamp": 1711234567
}
```

Key fields:
- `pay_per_obs_sats` — the total sats the host promises to pay out per observation; distribution across predictors is entirely the host's choice and visible in public channel commitments (KIND_34604), but the community's accountability check is simply: does the sum of payments per observation match this number?
- `paid_predictors` — intent: how many predictors the host plans to pay per observation (e.g. top 3)
- `competing_predictors` — intent: how many channels the host plans to open; the host may score thousands of predictors but only maintain this many active channels
- `scoring_metric` — baked-in scorer name (`"mae"`, `"rmse"`, `"directional_accuracy"`, etc.) or `"custom"`
- `scoring_params` — optional extra parameters passed to the scorer
- `horizon` — how many steps ahead the prediction covers
- `active` — set to `false` (or publish empty content) to close the competition

These fields are **intent metadata, not enforced constraints**. The host announces them as a social signal to the community — "here is what I plan to do." Nothing at the protocol level enforces them. A host who consistently misrepresents their intent becomes visible through payment records and loses predictors.

### KIND_PREDICTION Content (Encrypted DM to Host)

```json
{
  "stream_name": "btc-price-usd",
  "stream_provider_pubkey": "hex...",
  "seq_num": 1042,
  "predicted_value": 67450.25,
  "timestamp": 1711234560
}
```

Sent via NIP-04 encrypted DM so only the host can read it. A stream is uniquely identified by `(stream_name, stream_provider_pubkey)` together — name alone is not unique across producers. `seq_num` refers to the upcoming observation the prediction is for.

---

## Scoring and Payment Pipeline

This is the core framework. An observation on the primary stream is the trigger. The pipeline runs as follows:

```
1. Predictions arrive from predictors → saved, indexed by (stream, predictor)
2. Observation arrives on primary stream → TRIGGER
3. Gather most recent predictions (according to lag, default = 1)
4. Load scoring module by name (built-in or custom)
5. Pass payload to scoring module:
       - predictions (predictor_pubkey, predicted_value, timestamp, has_channel)
       - observation (the actual value that triggered this)
       - competition details (pay_per_obs_sats, paid_predictors, competing_predictors, etc.)
6. Scoring module returns: { predictor_pubkey → sats }
7. Execute payments:
       - predictor has a channel → send payment
       - predictor has no channel → open one, send payment
```

The scoring module is a black box. The framework defines only what goes in and what comes out — it does not care how scoring, ranking, or splitting happens internally.

### Scoring Module API

**Input payload** (dict passed to the entry point):

```python
{
    'predictions': [
        {
            'predictor_pubkey': 'hex...',
            'predicted_value': 67450.25,
            'timestamp': 1711234560,
            'has_channel': True,
        },
        ...
    ],
    'observation': 67500.00,
    'competition': {
        'stream_name': 'btc-price-usd',
        'stream_provider_pubkey': 'hex...',
        'pay_per_obs_sats': 300,
        'paid_predictors': 3,
        'competing_predictors': 5,
        'scoring_params': {},
        'horizon': 1,
    },
}
```

**Output** (dict returned by the entry point):

```python
{
    'hex_pubkey_of_predictor': 150,  # sats to pay this predictor
    'hex_pubkey_of_another':   100,
    'hex_pubkey_of_another':    50,
}
```

The sum of values should equal `pay_per_obs_sats`. The framework does not enforce this — it is the module's responsibility and the host's promise to the community.

### Custom Scoring Modules

Users drop a Python file into `neuron-lite/scoring/` — that is the entire installation process. The file must expose a single entry point function:

```python
def score(payload: dict) -> dict:
    ...
```

The `scoring_metric` field in the competition announcement is the filename (without `.py`). The neuron loads it with `importlib` at competition setup time and calls `score(payload)` on each observation.

Built-in modules ship in `neuron-lite/scoring/` alongside any custom ones. The loader makes no distinction — it just looks up the filename. A minimal template:

```python
# neuron-lite/scoring/my_scorer.py

def score(payload: dict) -> dict:
    predictions = payload['predictions']
    observation = payload['observation']
    competition = payload['competition']
    total = competition['pay_per_obs_sats']

    # your logic here — rank predictors, decide splits
    # return { predictor_pubkey: sats_to_pay }
    return {}
```

### Channel Opening

The neuron opens channels automatically when a scoring module returns payment for a predictor without an existing channel. Predictors may also open a channel to the host themselves — `has_channel` in the payload reflects this so the module can factor it in.

The social contract is simple: if the host is not paying, predictors stop predicting. No trial period or qualification window exists at the protocol level — the economics enforce behaviour naturally.

---

## Accountability Mechanism

The key accountability property: **payment commitments are public even though predictions are private**.

- Predictions: encrypted DM, only host sees them
- Payments: KIND_34604 channel commitments on Nostr, visible to everyone
- Anyone can subscribe to KIND_34604 events tagged with the host's pubkey and sum up payments per predictor
- If the host promises 100 SATORI/obs to the top 3 but only pays 1 person, that's visible
- Reputation is enforced by the community — bad hosts get no predictors

---

## Data Stream Announcement Updates

The existing `KIND_DATASTREAM_ANNOUNCE` (34600) is already parameterized replaceable (`d=stream_name`). Re-announcing is therefore already supported — a producer just publishes a new event with the same `d` tag and the relay replaces it. **This should be verified and surfaced in the UI** as an explicit "update stream" action rather than a hidden side effect.

---

## Stretch Goal: Authorized Predictors with Encrypted Data

For hosts who want to keep their data stream private (e.g. a company's internal metrics):

1. Host publishes the competition with `encrypted: true`
2. Host maintains an explicit whitelist of authorized predictor pubkeys
3. Host encrypts the data stream key and sends it to each authorized predictor via NIP-04 DM
4. Host rotates the key periodically and re-sends to all authorized predictors
5. Only authorized predictors can decrypt and therefore predict the stream

Over time, predictors can build a trust reputation — known to keep private data private, always submit predictions on time, never leak keys — making them desirable partners for encrypted competitions. This maps naturally onto the existing neuron credentialing work.

---

## Implementation Phases

### Phase 1 — Competition Announcement
- Add `KIND_COMPETITION_ANNOUNCE` (34607) to `models.py`
- `announce_competition` and `close_competition` methods on `SatoriNostr`
- `discover_competitions` query method
- DB table: `competitions` in `network_db.py`
- UI: competition announcement page

### Phase 2 — Prediction Submission
- Add `KIND_PREDICTION` (34608) encrypted DM to `SatoriNostr`
- Predictor side: `submit_prediction(stream_name, host_pubkey, seq_num, value)`
- Host side: `_handle_prediction_event` queues incoming predictions
- DB table: `predictions` (host stores all received predictions per seq_num)

### Phase 3 — Scoring and Payment
- Scoring engine: pluggable metric functions keyed by `scoring_metric` string
- After each observation: score all predictions for that seq_num, rank predictors
- Pay top N via existing `sendChannelPayment` (channels already built)
- Neuron automatically opens channels to predictors as they appear; host can override

### Phase 4 — Accountability Tooling
- Observer can subscribe to a host's KIND_34604 events and tally payments
- UI: competition leaderboard (payment totals per predictor, publicly derivable)
- UI: host reputation score based on payment consistency

### Phase 5 (Stretch) — Encrypted Streams + Authorized Predictors
- Host whitelist management
- Encrypted data stream key distribution via NIP-04 DM
- Key rotation flow
- Predictor trust/credential system

---

## What Is Already Built (No Changes Needed)

- Payment channels (open, pay, claim, reclaim) — fully implemented
- Encrypted observation delivery via NIP-04 — fully implemented
- `record_payment` / `record_subscription` access gate — fully implemented
- `sendChannelPayment` with `stream_name` — fully implemented
- Nostr relay infrastructure (multi-relay, dedupe, reconnect) — fully implemented

The prediction market builds entirely on top of this foundation. No changes to existing channel or data stream logic are required for Phase 1–3.
