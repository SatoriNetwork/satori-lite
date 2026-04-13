# Simulation — Step One

**Goal of this step:** run **one** Satori neuron in a Docker container
that mounts the current working-tree source, launched against the
docker-in-docker daemon this dev container talks to. Nothing else — no
second neuron, no wallets, no payments, no tests. Just prove we can
start one container and reach its web UI.

## Why we stage the source into `/shared`

This dev container (`repos0`) does not run Docker itself. It talks to a
sibling container (`docker-daemon`, image `docker:dind`) over
`DOCKER_HOST=tcp://docker-daemon:2376`. Every container we spawn runs
**inside that sibling**, not inside us. Volume mounts for spawned
containers are resolved on the daemon side, so a `-v /foo:...` flag
only works if `/foo` exists on `docker-daemon`'s filesystem.

The only path that exists on **both** `repos0` and `docker-daemon` with
the same contents is `/shared` — both containers bind-mount the host's
`~/shared` directory. Our code lives at `/code/Satori/...` which
`docker-daemon` cannot see. So we copy the current source into
`/shared/satori-sim/` before mounting it.

## Step 1 — stage the source

Copy the current working tree for both `neuron` and `satorilib` (the
neuron imports `satorilib`, so both need to be visible inside the
container):

```bash
mkdir -p /shared/satori-sim
rm -rf /shared/satori-sim/neuron /shared/satori-sim/satorilib
cp -a  /code/Satori/neuron    /shared/satori-sim/neuron
cp -a  /code/Satori/satorilib /shared/satori-sim/satorilib
```

(`rsync` is not installed in this dev container, so we use `cp -a`.
The preceding `rm -rf` gives us the "delete removed files" behaviour
of `rsync --delete` — without it, stale files from a previous copy
would linger. Re-run this block any time you want the container to
pick up edits from `/code/Satori/`.)

## Step 2 — build the dev image (once)

The neuron repo ships `Dockerfile.dev`, a slim Python 3.10 image that
installs system packages and Python requirements but **does not copy**
any application source. That's exactly what we want: mount the source
at run time instead.

```bash
cd /shared/satori-sim/neuron
docker build -f Dockerfile.dev -t satori-sim:dev .
```

`DOCKER_HOST` is already set to the inner daemon, so `docker build`
sends the context to `docker-daemon` automatically — no `-H` flag
required.

## Step 3 — run one neuron

```bash
docker run -d \
  --name satori-sim-neuron \
  -p 24601:24601 \
  -v /shared/satori-sim/satorilib/src:/Satori/Lib \
  -v /shared/satori-sim/neuron/neuron-lite:/Satori/Neuron \
  -v /shared/satori-sim/neuron/engine-lite:/Satori/Engine \
  -v /shared/satori-sim/neuron/web:/Satori/web \
  -v /shared/satori-sim/neuron/tests:/Satori/tests \
  satori-sim:dev
```

Mount targets match `Dockerfile.dev`'s `PYTHONPATH`
(`/Satori/Lib:/Satori/Neuron:/Satori/Engine:/Satori`) and the paths
`start.py` expects. Source directories on the left side of each `-v`
come from the staged copy under `/shared/satori-sim/`.

`-p 24601:24601` publishes the neuron's web UI port on
`docker-daemon`'s network interface at port 24601. `docker-daemon` in
turn has the outer-host mapping `14601 → 24601` already punched
through by the DinD compose file, so the same port is reachable from
three different vantage points:

| Reached from                    | URL                               |
|---------------------------------|-----------------------------------|
| This container (`repos0`)       | `http://docker-daemon:24601`      |
| Outer host (laptop browser)     | `http://localhost:14601`          |
| Another container on `docker-net` | `http://docker-daemon:24601`    |

## Step 4 — verify it's alive

From inside this container:

```bash
docker ps --filter name=satori-sim-neuron
docker logs --tail 50 satori-sim-neuron
curl -sS http://docker-daemon:24601/health
```

If `/health` returns something non-empty, the web server is up and the
source mounts are wired correctly. If it hangs or `docker logs` shows
import errors, the most common cause is a mount-target mismatch — walk
the `PYTHONPATH` entries against the `-v` flags.

## Step 5 — stop it

```bash
docker stop satori-sim-neuron
docker rm   satori-sim-neuron
```

The image stays cached; the next `docker run` is instant. Staged
source at `/shared/satori-sim/` is left in place.

## Notes for later steps

- **First-login wizard.** On a fresh start the neuron has no wallet or
  vault. Opening the web UI will walk through creating them. Nothing
  is persisted yet — that's fine for step one, whose only goal is "can
  we start the process and reach the UI". Persistence is a later step.
- **Live editing.** Because source is bind-mounted, editing files in
  `/shared/satori-sim/neuron/...` is picked up on the next request or
  the next process restart. Editing files in `/code/Satori/neuron/...`
  is **not** — those are on a different filesystem. Either re-run the
  `rsync` in Step 1, or do all editing directly under
  `/shared/satori-sim/` (more awkward from an IDE perspective).
- **`docker-daemon` reachability.** If `curl http://docker-daemon:...`
  fails with a DNS error, confirm this container is on the same
  Docker network as `docker-daemon` (it is by default — see
  `/code/devs/docker-compose.yml`).

That's it. One container, one port, source mounted from `/shared`.
Later steps will add a second neuron, a shared relay, wallets,
payments, and so on — but each will build on whatever the previous
step proved.
