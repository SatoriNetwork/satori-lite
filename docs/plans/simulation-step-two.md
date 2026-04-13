# Simulation — Step Two

**Goal of this step:** add a **second** Satori neuron alongside the
one that step one already brought up, each with its own fully
independent copy of the source tree (no shared files, no shared
wallet), and both web UIs reachable from the outer host at the same
time. Builds directly on `simulation-step-one.md` — read that first.

## Ground rules

- The existing neuron from step one **stays running untouched**. We
  do not restage, rename, or re-mount it.
- The second neuron gets its **own** source tree at
  `/shared/satori-sim-2/` — a sibling of `/shared/satori-sim/`, not a
  subfolder of it. Nothing shared between the two: not source, not
  wallet, not engine data, not tests.
- Both neurons keep the default **internal** UI port of `24601`
  (no `SATORI_UI_PORT` override).
- The second neuron publishes to a **different** DinD-side port so
  the outer host can reach both UIs simultaneously.

## Port plan

`docker-daemon` has two DinD→outer mappings that fit here:

| Neuron                 | Container             | Internal | DinD publish | Outer host URL          |
|------------------------|-----------------------|----------|--------------|-------------------------|
| 1 (from step one)      | `satori-sim-neuron`   | `24601`  | `24601`      | `http://localhost:14601` |
| 2 (added in this step) | `satori-sim-neuron-2` | `24601`  | `5000`       | `http://localhost:15000` |

(`14601:24601` and `15000:5000` are both pre-punched in
`/code/devs/docker-compose.yml`.)

## Step 1 — stage a second source tree

Leave `/shared/satori-sim/` alone. Create a sibling directory for
neuron 2:

```bash
mkdir -p /shared/satori-sim-2
rm -rf /shared/satori-sim-2/neuron /shared/satori-sim-2/satorilib
cp -a  /code/Satori/neuron    /shared/satori-sim-2/neuron
cp -a  /code/Satori/satorilib /shared/satori-sim-2/satorilib
```

After this, `/shared/satori-sim/` and `/shared/satori-sim-2/` are
byte-identical source-wise but physically distinct trees. Anything
the second container writes into its mounted source (most importantly
the `wallet/` directory created by the first-login wizard) lands
only in `/shared/satori-sim-2/`.

## Step 2 — image (no change)

The `satori-sim:dev` image built in step one is fine. It contains
only the Python environment — no source — so the same image can back
any number of containers mounting different trees. Nothing to do
here unless the image somehow went missing:

```bash
docker images satori-sim:dev
```

## Step 3 — run neuron 2

```bash
docker run -d \
  --name satori-sim-neuron-2 \
  -p 5000:24601 \
  -v /shared/satori-sim-2/satorilib/src:/Satori/Lib \
  -v /shared/satori-sim-2/neuron/neuron-lite:/Satori/Neuron \
  -v /shared/satori-sim-2/neuron/engine-lite:/Satori/Engine \
  -v /shared/satori-sim-2/neuron/web:/Satori/web \
  -v /shared/satori-sim-2/neuron/tests:/Satori/tests \
  satori-sim:dev
```

Note the `-p 5000:24601`: the container-internal port stays at the
neuron default (`24601`), but DinD publishes it on its own port
`5000`, which `docker-daemon` maps out to the outer host as `15000`.

## Step 4 — verify both are alive

```bash
docker ps --filter name=satori-sim-
docker logs --tail 30 satori-sim-neuron
docker logs --tail 30 satori-sim-neuron-2

# From inside this container:
curl -sS -o /dev/null -w 'neuron-1: %{http_code}\n' http://docker-daemon:24601/
curl -sS -o /dev/null -w 'neuron-2: %{http_code}\n' http://docker-daemon:5000/
```

From a laptop browser:

- Neuron 1 UI: <http://localhost:14601>  (the one from step one, unchanged)
- Neuron 2 UI: <http://localhost:15000>

Each UI walks through its own first-login / vault-setup wizard
independently. Neuron 2's wallet lands in
`/shared/satori-sim-2/neuron/neuron-lite/wallet/`; neuron 1's wallet
is untouched.

## Step 5 — stop neuron 2

If you only want to tear down what this step added (leaving the
step-one container alive):

```bash
docker stop satori-sim-neuron-2
docker rm   satori-sim-neuron-2
```

The staged tree at `/shared/satori-sim-2/` is left in place, so the
wallet it created survives a stop/start cycle.

## Notes

- **Re-staging overwrites the wallet.** Re-running step 1's `rm -rf`
  deletes the staged tree — including any wallet neuron 2 wrote
  there. `tar -czf` the `wallet/` directory first if you care about
  keeping it, or re-stage only when you want a clean slate.
- **Editing live code.** Edits under `/shared/satori-sim/...` affect
  only neuron 1; edits under `/shared/satori-sim-2/...` affect only
  neuron 2. Edits under `/code/Satori/...` affect neither until
  re-staged.
- **Neuron-to-neuron networking.** Both containers attach to
  `docker-daemon`'s default bridge network and can reach each other
  by container name (`satori-sim-neuron`, `satori-sim-neuron-2`) on
  any internal port. That's what later steps (relay, stream market,
  payments) will build on.
