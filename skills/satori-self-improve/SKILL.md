---
name: satori-self-improve
description: >-
  Use when a human wants to inspect, fix, or change their Satori neuron — answer
  through the neuron's REST API when you can, and when a request needs a code
  change, edit the neuron's live container source to give immediate relief, then
  submit that change upstream as a pull request so the whole network benefits.
  The Satori neuron runs in Docker with live Python source (not compiled
  binaries), so you can read and edit the exact code that is running.
---

# Satori Neuron — Self-Improvement

You are interfacing with a **Satori neuron**: one node among many in a
decentralized prediction network. It consumes datastreams, trains models, and
publishes predictions back to the network. It runs inside a **Docker container
whose source is live Python** (the code is copied in, not compiled to a binary),
which means you can read and modify the exact code that is running right now.

That property is the whole point of this skill. Most software you can only use;
this one you can **fix and improve while it runs**, and push that improvement out
to everyone.

## The loop you are part of

```
human wants something  ─►  you satisfy it
                              │
            ┌─────────────────┴─────────────────┐
   answerable via the API?            needs a code change?
            │                                   │
   call the endpoint,            edit the live container source
   report the answer             (immediate relief for this operator)
                                            │
                              submit the change upstream as a PR
                              to the community branch
                                            │
                       maintainers review & merge  ─►  CI rebuilds the
                       image  ─►  every neuron operator gets it on update
```

The community **asks**; you **deliver**; and every delivery that touched code
becomes a proposal for the entire network. You propose, the maintainers dispose.

---

## Step 1 — Discover the neuron and its API

The neuron serves a web UI and REST API. By default it is on the operator's
machine at **`http://localhost:24601`** (ask the operator for the host/port if
it is remote or remapped).

**Always start by fetching the API index.** It is the machine-readable list of
every endpoint the neuron exposes, grouped by category, and it tells you the
repo, the community branch, and the in-container source paths:

```bash
curl -s http://localhost:24601/api/index            # JSON
curl -s "http://localhost:24601/api/index?format=md" # Markdown table, easy to read
```

**Prefer the API over code changes.** A large fraction of requests are just a
question the API already answers — and answering them needs no PR at all:

| The human asks…                       | Just call…                          |
|---------------------------------------|-------------------------------------|
| "How many datastreams do I have?"     | `GET /api/engine/streams`           |
| "What's my balance / rewards?"        | `GET /api/balance/get`              |
| "Is the engine running? which model?" | `GET /api/engine/status`            |
| "How are my predictions scoring?"     | `GET /api/engine/performance`       |
| "What am I subscribed to?"            | `GET /api/network/subscriptions`    |
| "What bounties am I in?"              | `GET /api/bounties/mine`            |

Use `/api/index` to find the right endpoint instead of guessing.

---

## Step 2 — Find the container and its source

Only needed when the request requires a code change.

```bash
docker ps   # find the neuron container
```

The neuron container is typically named **`satori`**, **`satori-lite`**, or
**`satori-dev`**, running image **`satorinet/satori-lite`**.

Source paths **inside the container** (also reported by `/api/index`):

| Component            | Path in container        | Repo directory   |
|----------------------|--------------------------|------------------|
| Neuron runtime / CLI | `/Satori/Neuron`         | `neuron-lite/`   |
| Forecasting engine   | `/Satori/Engine`         | `engine-lite/`   |
| Web UI + REST API    | `/Satori/web`            | `web/`           |
| Shared library       | `/Satori/Lib/satorilib`  | `satorilib/src/` |

Read code with `docker exec`:

```bash
docker exec satori cat /Satori/web/routes.py
docker exec satori ls  /Satori/web/templates
```

---

## Step 3 — Make the change (live, for immediate relief)

Edit the file(s) inside the running container so this operator gets what they
asked for right away. Get the file out, edit it, put it back:

```bash
docker cp satori:/Satori/web/templates/dashboard.html ./dashboard.html
# …edit ./dashboard.html…
docker cp ./dashboard.html satori:/Satori/web/templates/dashboard.html
```

Apply the change:

- **HTML / CSS / JS templates** — a page refresh usually picks them up.
- **Python logic** — restart the neuron process to load it:
  `docker restart satori` (warn the operator about the brief downtime first).

**Verify it worked** before declaring success: re-hit the relevant endpoint or
reload the UI and confirm the human's actual need is met.

### Make the change persist (important)

By default the production image has the code **baked in and not mounted** (the
standard install runs with only a data volume), so a `docker exec` edit is
**ephemeral** — it is lost the moment the container is recreated or the image is
repulled. There are two ways to keep it:

1. **The PR (Step 4) — the real fix.** Once merged and CI rebuilds the image,
   the change is permanent for *everyone* on their next pull. This is the
   intended path; the in-container edit is just for immediate relief.

2. **Mount the source — to persist it on *this* machine right now.** The neuron
   must run with its source bind-mounted from the host so edits live on the host
   and survive restarts. Put the repo on the host (clone it, or `docker cp` the
   dirs out of the container) and re-run the container with mounts over the baked
   paths:
   ```bash
   -v /path/to/satori-lite/neuron-lite:/Satori/Neuron \
   -v /path/to/satori-lite/engine-lite:/Satori/Engine \
   -v /path/to/satori-lite/web:/Satori/web \
   -v /path/to/satori-lite/skills:/Satori/skills
   ```
   (The `satori-dev` service in `docker-compose.local.yml` already runs this
   way.) Then edit on the host and `docker restart`.

Without a mount your edit is ephemeral by design — so **always do Step 4** even
after a local fix, or the change disappears on the next update.

---

## Step 4 — Submit the improvement upstream (this is what makes it self-referential)

Your edit fixed *this* neuron. To fix it for **everyone**, the change has to go
upstream against the **community branch** (read the exact repo + branch from
`/api/index` → `self_improve.repo` / `self_improve.community_branch`).

**You do not need your own GitHub account, and you do not translate any paths.**
After you've edited the files in place (Step 3), just submit — the neuron diffs
your live edits against the pristine baseline it shipped with, emits a correct
**repo-relative** patch, and records the **base commit** so it applies cleanly
upstream. A single maintainer-side bot opens the actual PR. The container is not
a git repo and ships no credentials — do not try to `git push` from it.

### Path A — let the neuron build & submit the diff (default, no GitHub account)

```bash
# optional: preview exactly what will be submitted (diff, files, base commit)
curl -s -X POST http://localhost:24601/api/improve/diff | jq

curl -X POST http://localhost:24601/api/improve/submit \
  -H 'Content-Type: application/json' \
  -d '{"title":"Short summary","description":"What the operator wanted and how this delivers it."}'
```

The neuron auto-detects every file you changed under `neuron-lite`, `engine-lite`,
`web`, and `skills`. To scope it, pass the container paths you edited:
`{"files":["/Satori/web/templates/dashboard.html"]}`. You can still pass your own
`"diff":"<unified diff>"` if you'd rather build it yourself.

> Multi-repo: the neuron container holds two repos — the neuron itself
> (`satori-lite`: `/Satori/Neuron`, `/Satori/Engine`, `/Satori/web`,
> `/Satori/skills`) and the shared library (`satorilib`:
> `/Satori/Lib/satorilib`). The neuron auto-detects which repo each edit belongs
> to and **opens a separate PR per repo** to the correct repository — you don't
> route anything. (`POST /api/improve/submit` returns one entry per repo in
> `submissions`.)

### Path B — open the PR yourself (optional, only if you already have GitHub auth)

If the environment already has `gh`/git authenticated, you may skip the handoff
and open the PR directly instead:

```bash
gh repo fork SatoriNetwork/satori-lite --clone   # or use your existing fork
cd satori-lite && git checkout <community-branch> && git checkout -b improve/<desc>
# apply your change here (e.g. docker cp the edited file out into web/<f>)
git commit -am "<what changed and why>"
gh pr create --repo SatoriNetwork/satori-lite --base <community-branch> \
  --title "<title>" --body "<what the operator wanted and how this delivers it>"
```

---

## Guardrails

- **Never** read, move, or transmit wallet files, vault files, private keys,
  seed phrases, or session secrets. They are not yours and never belong in a
  diff, a log, or a PR.
- Keep diffs **small and focused** — one change for one request. No drive-by
  refactors or unrelated edits.
- **Explain** to the operator what you changed, why, and that you've proposed it
  upstream so the whole network can get it.
- You **propose**; the maintainers **review and merge**. Don't assume your change
  is final, and don't try to bypass review.
- When in doubt about whether a request needs code at all, re-check `/api/index`
  first — the cheapest fix is the one that needs no PR.

---

## Quick reference

| Thing                     | Where                                            |
|---------------------------|--------------------------------------------------|
| This skill (raw)          | `GET /api/skill`                                 |
| API index (all endpoints) | `GET /api/index` (add `?format=md` to read)      |
| Preview your edits' diff  | `POST /api/improve/diff`                          |
| Submit upstream           | `POST /api/improve/submit` (auto-builds the diff) |
| Native MCP access (opt.)  | `/Satori/mcp-server/` — same tools over MCP; see its README |
| Repo & community branch   | `/api/index` → `self_improve.repo` / `.community_branch` |
| In-container source       | `/Satori/Neuron`, `/Satori/Engine`, `/Satori/web`, `/Satori/Lib/satorilib` |
| Web UI / API port         | `24601`                                          |
