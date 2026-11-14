# Satori Self-Improvement — How It All Works

## The idea in one breath

A Satori neuron runs in Docker as **live Python source, not a compiled binary**.
That single fact is what makes everything possible: an AI can read and edit the
exact code that is running. So the neuron is a **self-referential,
community-guided improving system**. A person asks their own AI to fix or change
something about their neuron; the AI does it; and if doing it required a code
change, that change is proposed **upstream as a pull request**. Once a maintainer
merges it, CI rebuilds the image and every operator gets it on the next pull. The
community asks, the AI delivers, and every code change becomes a proposal for the
whole network. Nothing merges without a human.

## The pieces

Three code bases. The **neuron** (`satori-lite`) is the node. **central**
(`central-lite`) is the server the neuron already talks to. **satorilib** is the
shared library both depend on, and it also lives inside the neuron container, so
it is improvable too. The operator's **AI** runs outside the neuron and reaches
it over plain HTTP or over MCP. central runs on your servers and holds the one
GitHub credential that matters.

## Step 1 — Discovery

Two public endpoints bootstrap everything. `GET /api/skill` returns the
self-improvement skill — a document that teaches the AI the whole workflow.
`GET /api/index` returns a live, machine-readable map of every endpoint the
neuron exposes, grouped by category, generated from the real route table so it
can never drift. The guiding rule: prefer answering from the API over changing
code, because most requests are just a question an existing endpoint already
answers and need no PR.

## Step 2 — The MCP server

The neuron is also a native tool in MCP clients (Claude Code, Claude Desktop) via
a bundled MCP server. It is a thin wrapper over the same REST API, holds no
credentials, and runs where the AI runs. It offers navigation (`get_api_index`,
`find_endpoints`), a generic `call_endpoint` to drive the whole neuron, the
self-improvement tools, and the reuse tools below. It runs over stdio by default
or as an HTTP service.

## Step 3 — Authentication, without the vault password

Most endpoints require an unlocked session, which exists only because the
operator logged in through the browser, where the wallet is decrypted. The AI
never handles the vault password. Instead the operator passes their existing
session cookie to the MCP server (`SATORI_NEURON_COOKIE`); the server never sees
the password. Missing/stale session returns a clear `auth_required` result, not
login HTML. Public endpoints (skill, index, improve) work with no cookie.

## Step 4 — Security: what the AI can never get

Two things are sensitive: private keys and the vault password. The password is
never taken. Endpoints that reveal key material — the wallet key, the
identity/nostr key, the wallet-file download — are hard-blocked at the MCP layer,
refused before any request reaches the neuron, and hidden from the index even
with a valid session. The operator can still see their own keys in the UI.
Separately, wallet and data files are not even present in the source baseline, so
they cannot be accidentally diffed into a PR.

## Step 5 — Reuse before you build

When a request needs a code change, the AI first checks whether someone already
proposed it, instead of reinventing a fix. `GET /api/improve/open` lists existing
**open proposals** (unmerged PRs), ordered by how many operators have adopted
them — popularity is a strong signal it works. `GET /api/improve/proposal/<id>`
returns a candidate's diff, files, and PR url so the AI can evaluate it. If it
fits, the AI applies that proposal's diff to the live code (so the operator gets
it now, before merge) and records the adoption with `POST /api/improve/adopt/<id>`.

Adoption is the popularity loop: central counts **distinct operators** (one per
wallet) who applied each proposal and notes the running tally on the PR itself.
So maintainers can see which unmerged proposals are broadly useful — battle-
tested across several neurons — and merge the best ones sooner. Only if nothing
suitable exists does the AI build a new change.

## Step 6 — Making a change persist

The AI edits the live files for immediate relief. But production bakes code into
the image, so an in-container edit is temporary and disappears on the next image
pull. The durable path is the pull request. For local persistence right now, the
operator can bind-mount the source and restart. The skill is explicit: always do
the PR, or the change is lost on update.

## Step 7 — From a live edit to the right pull request(s)

The image ships a pristine baseline of every source tree plus each repo's build
commit. When the AI submits, the neuron diffs the live files against the baseline
and produces a clean, repo-relative diff with the correct base commit recorded —
the AI never translates paths or picks a base. The neuron also groups changes by
repo: files under the shared library map to the satorilib repo, everything else
to satori-lite. A single edit session that touches both produces two diffs.

The AI calls `POST /api/improve/submit` with a title and description (it can
preview first with `POST /api/improve/diff`). The neuron creates one submission
per affected repo, keeps a local audit copy, and forwards each to central over
the neuron's existing wallet-authenticated channel — so central knows which
wallet submitted it, without the AI handling any credential. If central is
unreachable, the local copy is kept and the response says it was not forwarded,
so nothing is lost.

## Step 8 — Central turns diffs into PRs (the one credential)

central validates each submission: a size cap, a repo allow-list (so an AI cannot
target an arbitrary repo), and a block on any diff touching CI config. It queues
the change. A worker runs every couple of minutes and, for each item, clones the
correct repo using the single central `GITHUB_TOKEN` (which never leaves
central), checks out the recorded base commit, applies the diff, commits with the
bot identity and a note recording the submitting neuron, pushes a branch, and
opens a pull request against that repo's community branch. Success or failure is
recorded; failures keep their error. Nothing is auto-merged — human review is the
backstop for everything.

## Step 9 — Keeping multi-repo changes straight

When one proposal spans both repos, the two submissions share a group id. Each PR
body states that it is part of that group and names the companion repo, asking
you to evaluate them together. Once both PRs are open, the bot comments on each
with the other's PR url, routed to the right repo, exactly once. This waits until
both exist, so it works whether they open together or apart.

## Step 10 — Status back to the human

The neuron proxies central's status, returning the state
(`queued → processing → pr_open | failed`), the PR url once open, the repo, and
the group id — so the AI can hand the operator the link and point at the
companion PR.

## The loop, closed

Human asks → AI answers from the API, or reuses an existing proposal, or edits
live code → neuron builds and routes diffs per repo → forwards (wallet-authed) to
central → central opens human-gated PRs that cross-reference each other within a
group → adoptions accumulate as a popularity signal → a maintainer merges → CI
rebuilds the image → operators pull it → the improvement is live for the whole
network.

## Situations handled

- Pure question, no code: answered via the API; no PR.
- An existing fix already proposed: found via search, evaluated, adopted, and
  counted — no duplicate work.
- Neuron-only, satorilib-only, and both-at-once changes all route correctly.
- Ephemeral edits: covered by the PR, plus the bind-mount option for local
  persistence.
- Private keys: blocked at MCP before reaching the neuron, and hidden from the
  index, even with a valid session.
- Vault password: never handled; operator supplies a session cookie instead.
- Arbitrary-repo PRs, CI tampering, and secrets in diffs: all prevented.
- One credential: the only GitHub token lives on central, never in images or
  neurons.
- central unreachable: submission kept locally; nothing lost.
- A diff that will not apply: marked failed with its error, never half-merged.
- Running without a baseline: auto-diff reports unavailable; a raw diff is
  accepted instead.
- Provenance and abuse: every submission and adoption is wallet-attributed and
  human-gated.

## Decisions worth explicit sign-off

1. Fund-moving endpoints (wallet send, lender pay, channel pay) are not blocked
   at the MCP layer, per the guidance that only keys are sensitive. An
   authenticated agent could move funds; blocking or confirming those is a small
   addition if wanted.
2. central itself is not improvable through this loop, because it is not present
   in the neuron container.
3. satorilib base-commit accuracy depends on CI passing the satorilib build
   commit; without it, satorilib diffs target the branch tip and may need a
   manual rebase.
4. Linkage between companion PRs is the body text plus the cross-link comment; no
   GitHub labels or auto-close rules were added.
5. Adoption notes post one PR comment per new distinct adopter; if that is noisy
   at scale, switch to milestone-only comments.
6. To activate: set `GITHUB_TOKEN` on central with PR rights on both repos, apply
   the database migration, and make sure the community branch name matches what
   CI watches.
7. Any operator with a wallet can submit and adopt. Defenses are human review, CI
   protection, the repo allow-list, and a size cap; a per-wallet rate limit or a
   trusted-wallet allow-list is the next lever if abuse is a concern.
