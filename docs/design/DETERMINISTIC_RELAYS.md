# Deterministic Relay Assignment

**Status:** Parked. Analysis concluded the current policy is near-optimal under existing constraints (no popularity data, small relay set, hard subscriber-connection cap). Code work deferred until we have load/popularity signal; this doc records the reasoning so we don't re-derive it.
**Branch:** `deterministic-relays` (neuron + satorilib) — kept for reference, no code landing.
**Original goal:** Replace today's "loop through all relays until subscribed" policy with a deterministic stream-to-relay mapping that spreads connection load across the relay set without requiring runtime coordination with a central authority.

---

## 1. Problem statement (original message, verbatim)

> That's what I've been thinking about.
>
> They loop through all relays until they are subscribed to the streams they want. Then they publish all predictions to whatever relays they happen to be connected to.
>
> That's not optimal. But it's hard to do much better with the constraints we have: relays are assumed to be community owned and therefore not reliable: they can go down or come up at any time, and they can't handle too many connections.
>
> That's our bottleneck: number of concurrent connections per relay, not number of streams per relay which is what the AI will naturally optimize for if you work with it on this problem.
>
> So that's almost the best policy we can make with those strict constraints. Which is why I didn't change anything or try to improve it yet.
>
> But if we assume that relays are mostly stable there's a simple policy that we can use to optimize I think.
>
> Since relays cannot handle many connections: like 500 I heard from WillQ, it's our goal to spread out connections. Therefore we would map each user or stream to two or more deterministically random relays by modulo range. That way if relay count changes the relay connections slowly change too.
>
> So instead of looping through all relays you would know exactly which 2 you could connect to to find each datastream you want. You would try one then the other. And you would post your predictions there but any raw datastreams you'd post to your assigned relays.
>
> That would help spread out connections. But also requires Neuron connections to move as relays come online and offline. But the benefit is, you don't need to ask the central server for who is assigned to which relays, you can compute it yourself.
>
> If we knew the popularity of streams we could spread out out even better: extremely popular streams would be posted on many relays, unpopular streams would be posted on one or two. But we don't have to start with that.
>
> Lastly, another optimization we can make is to reduce connections on relays during anticipated silence. This is a client side thing, if you know the streams are a daily cadence and you don't really care when predictions are made as long as it's within a day, then you could connect once a day, get all updated values because the last value remembered on relay, make your predictions, then disconnect till same random time tomorrow. It's not a good optimization for the marketplace so I'm not seriously considering it.
>
> Ok so, I've already made some code on some branch to actually choose the right relays to publish to but I haven't actually created the whole feature. I could finish it so we could test it now if you prefer?
>
> I'll call the branch deterministic-relays. It actually increases connections slightly but it spreads them out among all relays and decreased connections after scale. We need like 4-10 relays to test this properly. We only have 2 so everything would look the same even if we implemented this.

---

## 2. Initial review (verbatim)

> The core idea is right — connections-per-relay is the real bottleneck, and deterministic mapping beats stochastic spreading because clients can look up directly rather than search. A few things I'd push back on or sharpen before you finish the code:
>
> **1. Don't use modulo. Use rendezvous (HRW) hashing.**
>
> This is the most important point. Your intuition was "if relay count changes the relay connections slowly change too" — but that's not how modulo works. With `hash(stream) % N`, going from N=4 to N=5 reshuffles ~80% of assignments. Mass migration on every relay add/drop is the opposite of what you want, especially in a community-owned relay set where churn is the default.
>
> Rendezvous hashing (a.k.a. HRW) fixes this with the same conceptual simplicity:
> ```
> for each relay r in R: score = hash(stream_id, r.id)
> pick the top-k relays by score
> ```
> - Add a relay → only ~1/N of streams move (the ones for which the new relay scores highest).
> - Drop a relay → only the streams it was hosting redistribute, and they spread evenly across the survivors.
> - Clients still compute it independently — no central coordination required.
>
> Consistent hashing (ring) works too but is fiddlier; HRW is ~5 lines of code and statistically better with small N.
>
> **2. "No central coordination" is overstated — the canonical relay set IS the coordination point.**
>
> Two clients compute the same top-k only if they agree on three things: the relay set R, the hash function, and k. The discovered_relays table is per-central today; for clients to agree on assignments, they have to agree on the list. So you've moved the coordination question from "who hosts what" to "what's the canonical relay list" — not eliminated it. Probably fine (the relay list churns slowly and clients already pull it from NIP-65), but worth being honest about.
>
> **3. Decide stream-mapping vs. user-mapping deliberately.**
>
> You mentioned both in the same sentence; they have different load shapes:
> - **By stream**: a user with N streams opens up to N×k connections, but load is maximally spread per stream. Best for the relays.
> - **By user (publisher)**: a user opens k connections total and writes all their streams there. Best for the client, but a popular publisher concentrates on their assigned relays.
>
> For raw datastreams I'd go stream-keyed — the goal is *spreading* — and accept the per-user connection cost. Subscribers benefit too: they look up by stream id, not by hunting through the publisher's connections.
>
> **4. The asymmetric publish policy is interesting but think it through.**
>
> "Predictions go to whatever relays you happen to be on, raw datastreams to assigned relays." This works only because the prediction-tallying service has to sweep all relays anyway. Net effect: writers spread load, but any non-central reader of predictions has to sweep too. If the only reader of predictions is central, fine. If neurons need to read each other's predictions directly, this asymmetry breaks the locality benefit. Worth being explicit about which it is.
>
> **5. You lose free self-healing.**
>
> Today's "connect to whatever works" has built-in failure tolerance — if a relay misbehaves you just don't connect to it. Deterministic assignment loses that: you keep hitting your assigned relay even if it's degraded. You'll need to add:
> - Explicit fallback order (which is what k>1 buys you — try #1, on failure try #2)
> - Health tracking (consecutive failures, success rate, etc.) so you know when to fall through
> - A "the relay set really did change" signal vs. "this relay is just having a bad day" signal — easy to confuse them, and confusion causes thundering-herd remappings.
>
> **6. The "popular streams on more relays" extension is k_replicas(stream) = base + f(popularity).**
>
> That's just HRW with per-stream k. Don't build it now. Build it after you have observed which streams are hot. It's a one-line change to the assignment function later.
>
> **7. The 4-10 relay test bed is genuinely blocking.**
>
> You're right that with 2 relays, k=2 means every client connects to every relay anyway — indistinguishable from current behavior. Before you finish this work, get clarity on how you'll spin up 4+ relays for the test. Otherwise the feature ships unverified.
>
> **Net:** the plan is good and worth shipping. Use HRW not modulo, decide stream-vs-user mapping explicitly, plan the fallback/health logic alongside the assignment logic, and line up the test bed first.

---

## 3. Decisions so far

| # | Topic | Decision |
|---|---|---|
| 1 | Hash algorithm (if any deterministic policy is used) | **HRW (rendezvous) hashing.** Modulo rejected — too much reshuffling on relay set changes. |
| 2 | "No central coordination" | Clarified: clients compute assignments themselves. They still need to agree on the relay set, but that's existing NIP-65 discovery, not a new authority. |
| 3 | Stream vs. user keying — and the bigger reframe | See §4 below. The framing "where should publishers send?" turned out to be the wrong lever. The cap is a subscriber-connection problem, and the current emergent policy (publish to everywhere you're connected) is already near-optimal for the popular head of the distribution. |
| 4 | Random-shuffle vs. deterministic subscriber ordering | **Random per-attempt wins.** See §5. |
| 5 | Disposition | **Park the branch (Option A).** Document the analysis; no code changes. Revisit when we have load/popularity data. |

---

## 4. Bottleneck re-analysis — why deterministic publishing is the wrong lever

(Conversation transcript, verbatim.)

### User — stream/subscription distributions are power-law

> It's a power law distribution of course which makes it difficult there will be some publishers which publish lots and lots of data streams but most might publish just predictions streams which are basically as many as I subscribe to and son might publish all their predictions streams plus a couple other raw data streams... On top of that the subscriptions are also power law with some datastreams being subscribed to by almost the whole network, others not subscribed to at all. This is the difficulty... Those streams that are popular have lots of subscriber connections. The bottleneck is relays have open connections so they can push to the neurons... So that's our choke point. Some can only handle 500 concurrent connections. So the current approach, publish everything you publish to everything you are connected to... Honestly given these constraints ( two laws, not knowing relay limitations, not knowing which datastreams are popular) I'm not sure we can do better than we are doing

### Response — the cap is a subscriber problem; deterministic publishing breaks popular streams

> You're right — and I think the analysis reveals something important: **the deterministic-publish framing might be the wrong lever entirely.** Let me try to unpack it.
>
> **The cap is a subscriber-connection problem, not a publish-target problem.**
>
> A relay's 500 concurrent-connection ceiling is dominated by long-lived subscriber websockets, not by publishers. Publishers can post-and-disconnect, or hold one connection that emits many events. So the question "where should publishers publish?" doesn't directly relieve the bottleneck. The thing the cap actually limits is "how many subscribers can a relay simultaneously push to."
>
> Once you see it that way:
>
> **The current "publish everywhere" policy is doing the right thing for the popular head of the distribution.** A stream with 10,000 subscribers needs to be on as many relays as possible, because subscribers must be allowed to spread out — if it's only on one or two relays, those two relays get blown past the cap and the rest of the network can't subscribe at all. Maximum replication of popular streams is the correct answer. The current loop-and-publish accidentally implements it.
>
> **Deterministic publish targeting actively breaks that property.** If you HRW-assign stream S to k=2 relays, then a popular S can only ever be served by those 2 relays — every subscriber must connect to one of them, and the cap gets hit hard. The "spreading" you get by varying which 2 across streams doesn't help any individual hot stream.
>
> **For the long tail (unpopular streams), deterministic mapping is fine but ~free either way.** Few subscribers, low connection pressure. Maximum replication wastes a tiny bit of relay storage; deterministic mapping saves it. Neither moves the needle.
>
> **Where the real lever sits: subscriber-side selection.**
>
> The leverage point isn't "where does the publisher send the event" — it's "which relay does each subscriber connect to." That's the thing that consumes the cap.
>
> If every stream is on every relay (current behavior), a subscriber for stream S can pick *any* relay. Today they pick by trial order, which tends to clump. The fix that matches your goal — deterministic, no central coordination, spreads load — is:
>
> ```
> for subscriber wanting stream S:
>     candidates = HRW(S, all_relays)            // deterministic permutation per stream
>     salt = hash(my_pubkey)                      // each subscriber gets a different cycling
>     try candidates[salt % len], then next, ...
> ```
>
> That gives you:
> - For each stream, subscribers spread across all relays in a deterministic permutation.
> - Different streams permute differently, so no single relay becomes "the favorite."
> - Different subscribers within the same stream start at different points, so they don't all pile on candidate #1.
> - Fallback is natural (just walk the list).
> - Publishers don't change at all. The existing emergent "publish to what I'm connected to" stays — which is fine because, for popular streams, that converges on max replication.
>
> This is a much smaller change. And it sidesteps every hard problem we'd hit on the publish side (asymmetric policies, fallback on publish failure, the head/tail mismatch above).
>
> **What you can't avoid: you need popularity data eventually.**
>
> The honest answer to "what's the optimal policy?" is "replicas-per-stream proportional to subscribers-per-stream" — but you have neither popularity nor relay-load data today, so you can't compute that. The unlock isn't a smarter algorithm; it's instrumentation. If relays publish their own load (open conn count, top-N streams by subscriber count) on a known kind, every client gets the signal they need to balance smarter. That's the unblocker for any future load-aware policy.
>
> **Proposed reframe of this branch:**
>
> Rather than "deterministic publish targets," make it:
> 1. **Subscriber-side deterministic relay selection** (HRW + per-subscriber salt). Real, immediate win on connection clustering. Cheap to implement, cheap to reverse.
> 2. **Relay self-reporting of load** as a known Nostr kind. Doesn't change behavior today but unlocks every future policy.
> 3. **Defer publisher-side mapping entirely** until #2 gives us the data to know whether it'd help.
>
> Branch is fine to keep the name; the *scope* changes. Want me to revise the design doc to reflect this redirect, or push back on any of it first?

---

## 5. Random-shuffle vs. deterministic-with-salt — random wins

### User — challenge on the "trial order clumps" claim

> "Today they pick by trial order, which tends to clump." Does it though? Subscribes always randomize their lists before looping to subscribe. You're suggesting a static randomization per subscriber. Of the two options isn't always random less clumpy?

### Response — conceded; deterministic ordering has no spreading advantage; branch should be parked

> You're right and I was sloppy. Let me work through the math because conceding on instinct isn't useful.
>
> **Random-per-attempt:** each subscriber rolls a fresh shuffle. For N subscribers and M relays, first-tries are uniform: each relay gets ~N/M. Retries get a fresh shuffle too, so if relay X is at cap, the next attempt automatically lands on a different random relay — natural load shedding.
>
> **HRW-with-per-subscriber-salt:** each subscriber has a deterministic first-try. If the salt is uniform across subscribers, the *expected* distribution is also uniform — ~N/M per relay. But retries are deterministic too, so if relay X is at cap, every retrying subscriber to that stream tries the same second-choice next. Slight thundering-herd flavor on failover.
>
> So in expectation random is at least as good for spreading, and strictly better at failure recovery (independent re-rolls instead of correlated fallback). I was wrong to call random "clumpy."
>
> The remaining genuine advantages of deterministic ordering are:
> 1. **Stable assignment** — same subscriber returns to same relay across reconnects, so warm websockets / cached subscriptions stick. Real but marginal.
> 2. **Predictability for debugging** — "where is subscriber X for stream S?" is answerable from the hash, not from the latest die roll. Operational nicety, not load-shaping.
>
> Neither is worth a redesign.
>
> **So where does that leave the branch?**
>
> I think the honest read is: **at current scale and current knowledge, you're correct that we can't meaningfully beat the existing policy.** The case for deterministic mapping only opens up when:
> - The relay set gets large enough that "publish everywhere" hurts (storage, publisher connection count) — not a real concern at 4–10 relays.
> - We have popularity data to do replicas-proportional-to-demand — and we don't.
>
> The thing that would *actually* move us forward isn't the assignment algorithm; it's getting the signal we'd need to know whether assignment matters at all. Concretely: have relays publish their own load (open connection count, top streams by subscriber count) on a known Nostr kind. That's a 50-line change with no behavior impact, but it unlocks every future policy decision — including whether deterministic mapping is even worth doing.
>
> Two paths for the branch:
>
> - **Option A: Park it.** Update the design doc to record the analysis (current policy is near-optimal under these constraints; deterministic mapping deferred until we have data). Don't ship code on this branch.
> - **Option B: Repurpose it for instrumentation.** Use the branch to add relay self-reporting of load. That's the unlock for everything downstream, and it's small enough to land cleanly.
>
> Which feels right? My lean is B — getting the signal is worth more than any policy we could pick without it.

---

## 6. Conformant load signal — what NIPs help

### User — picks Option A; asks whether any NIPs cover the load-signaling need without forcing custom relays

> Option A - just update it's document with the rest of this conversation. Including this message and you're next answer to my next question: are there any nips that help with this need for knowing load? We employ nip 11 on our relays very popular among public relays, if they do nip11 they can get paid, but we don't ever want to be in a situation unless we have to that our relays are necessarily custom. But if there are nips that help us get the data we need for better policies then we could encourage conformity to them.

### Response — NIP survey + NIP-66 monitor pattern as the conformant path

> Short answer: no NIP gives you live relay-self-reported load. But there's a clean conformant path using two NIPs already in the ecosystem.
>
> **Relevant NIPs:**
>
> - **NIP-11 (Relay Information Document)** — what you already use. The `limitation` field includes `max_subscriptions`, `max_filters`, `max_message_length`, `max_subid_length`, `max_limit`, plus auth/payment flags. This advertises the relay's *advertised cap* — the denominator you need for any load calculation. It does *not* report current usage. NIP-11 is static-doc by design, though nothing prevents a relay from updating it dynamically (some do — some return live event counts in the doc). Useful but only half the picture.
>
> - **NIP-45 (COUNT)** — replaces `REQ` with `COUNT` to get event counts cheaply. Doesn't help with connection load, but it's the right primitive for measuring *stream popularity* server-side (events per (kind, author) over a window). When you eventually want popularity-weighted replication, this is the standard query.
>
> - **NIP-66 (Relay Discovery and Liveness Monitoring)** — this is the interesting one. It defines two kinds:
>   - **Kind 30166** (relay discovery / monitor reports): an agent that monitors relays publishes parameterized-replaceable events keyed by relay URL, with tags for RTT, supported NIPs, cached NIP-11 fields, geographic info, last-seen, etc.
>   - **Kind 30066** (monitor announcement): a monitor declares itself — "I probe relays in this region at this cadence and emit these metrics."
>
>   The model is: external probers publish observations; clients subscribe to the observations rather than probing themselves. Exactly the pattern you want for load data that the relay itself can't be asked to emit.
>
> - **NIP-86 (Relay Management API)** — admin-side; not relevant for client load awareness.
>
> **The conformant path that avoids custom relays:**
>
> 1. **Read NIP-11 `limitation.max_subscriptions`** as the cap denominator. You probably already pull NIP-11 in the relay scoring flow; just retain that field.
>
> 2. **Have Satori central operate as a NIP-66 monitor.** It already probes relays for scoring purposes — extend that to record observable load proxies (connection-establishment latency, time-to-EOSE under load, subscription rejection rate, RTT) and publish kind 30166 events keyed by relay URL. Neurons subscribe to those reports the same way they subscribe to any other relay event.
>
>    What you can observe from outside without relay cooperation:
>    - **Latency under probe** — slow RTT correlates with load, though noisily.
>    - **Subscription rejection** — if the relay sends `CLOSED` on a REQ citing capacity (`error: too many subscriptions`), that's a direct cap-hit signal. Some relay implementations expose this.
>    - **NIP-11 dynamic fields** — if a relay chooses to include live event-count or connection-count in its info doc (some do), the monitor relays that forward.
>
>    What you *can't* observe externally: actual current connection count. Only the relay knows that.
>
> 3. **(Optional, future) Encourage NIP-11 dynamic fields.** If you're paying relays anyway, you can make richer NIP-11 (including a non-standard `current_subscriptions` or similar) part of the payment criteria. This is the "encourage conformity" path you mentioned — not a NIP yet, but a small, non-disruptive addition relays can opt into. If it works for you and a few others, propose it as a NIP-11 extension.
>
> So the answer to your underlying question — "can we get useful load data while staying standards-conformant?" — is yes, via the **NIP-66 monitor pattern with central as the monitor**, using observable proxies. Live self-reported load from the relay is not in any NIP today; the closest legitimate path is to push for a NIP-11 extension, and that's a longer-cycle effort.

---

## 7. Conclusion

The current emergent policy ("publish to every relay you happen to be connected to" + "randomize the relay list before looping to subscribe") is near-optimal under present constraints:
- Power-law stream popularity → popular streams must be on many relays; the current policy delivers that for free.
- Power-law publishing → no advantage to publisher-keyed mapping.
- 500-conn cap is a subscriber bottleneck → random per-attempt subscriber selection already spreads uniformly and self-balances under failure.
- Small relay set (2–10) → "publish everywhere" is cheap; partial replication offers no savings.

Deterministic mapping would only become worthwhile when (a) the relay set is large enough that full replication is costly, OR (b) we have popularity data to drive replicas-proportional-to-demand. Neither holds today.

**Next concrete step (when prioritized): pursue the NIP-66 monitor pattern.** Stand up Satori central as a relay monitor, publish observed-load reports on kind 30166, and let clients consume them. That's the unblocker for every future load-aware policy and avoids forcing relays into custom Satori-only behavior.

This branch (`deterministic-relays`) remains in place as a historical anchor for the analysis but will not receive code.
