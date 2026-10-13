# Public deployment of the simulation

The digital twin, the agent harness and the dashboard run on one small Google Cloud VM, as they do on the
Mac: the same code, the twin at 10×, the harness on the twin, and the agent on `claude-opus-5-5` with the
owner's API key. Visitors open the dashboard, start a run, and watch it live or replay recorded runs.

```
browser ──https──▶ Caddy :443 ──▶ harness 127.0.0.1:8750 ──▶ twin 127.0.0.1:8743
                  (Let's Encrypt)   dashboard, /api/*,        /v1/* (the agent's API),
                                    /rig/* pass-through       /sim/*, /operator/*
```

Only Caddy listens publicly. The dashboard reaches the twin through the harness (`/rig/...`), and only the
twin's read endpoints and the dashboard's own controls are passed through. The agent's API (`/v1/*`) is not
reachable from outside.

## Deploy

```bash
gcloud auth login                 # if gcloud asks for it
deploy/gcp.sh create              # own network, static address, Debian 13 e2-small VM
deploy/gcp.sh push                # code, Python environment, services, Caddy; prints the URL
deploy/gcp.sh key                 # ANTHROPIC_API_KEY from .env → /etc/aisim/secrets.env (root only)
```

`push` again after changing code (recorded runs, twin sessions, the limits and the key stay). The URL is
`https://<address with dashes>.sslip.io/`. sslip.io resolves that name to the address, so Caddy can get a
certificate without a domain. If Let's Encrypt refuses (sslip.io shares one rate limit with all its users),
`http://<address>/` keeps working; `deploy/gcp.sh push off` serves plain HTTP on both. A domain of your own
works the same: point an A record at the address and pass the name to `setup_vm.sh` instead.

Other commands: `deploy/gcp.sh url | status | logs [n] | ssh | stop | start | delete`.

## Limits (public mode)

`python -m harness --public` (set in `aisim-harness.service`) adds, from `harness/limits.py`:

| Limit | Default | Setting in /etc/aisim/aisim.env |
|---|---|---|
| Runs at a time | 1. Others watch it live; only the browser that started it can stop or message it, or change the twin's speed, faults and operator answers while it runs | |
| Cost per run | $6 (the 4 × 4 Arrhenius run cost $2.49) | `AISIM_RUN_USD` |
| Agent work per run | 90 min wall clock (time spent waiting for a message after the report doesn't count) | `AISIM_RUN_MIN` |
| Spending per day (UTC) | $40, then no new runs until midnight UTC | `AISIM_DAY_USD` |
| Runs per visitor per day | 4 (counted by a salted hash of the address) | `AISIM_IP_RUNS` |
| Prompt / message length | 6000 / 2000 characters | |

After editing: `sudo systemctl restart aisim-harness`. The file also holds `AISIM_ADMIN_TOKEN`: opening the
site once as `/?admin=<token>` lifts the limits in that browser (it can also stop anyone's run).

A run on a public twin starts from a fresh rig ("fresh rig" ticked): empty wells, a cold bed, the printer not
homed.

## What protects the key and the rest of the project

- The VM sits in a network of its own (`aisim-net`, open on 80, 443 and 22 only), so it can't reach the
  project's other VMs, and it has no service account: nothing on it holds Google credentials. Project-wide
  SSH keys are blocked on it; `gcloud compute ssh` adds yours to the instance only.

- The key is in `/etc/aisim/secrets.env` (root, mode 600). systemd hands it to the harness process only.
- The agent's `python` tool runs in bubblewrap (`harness/sandbox.py`): no network, its own process and
  user namespaces, and a file system holding only `/usr`, the Python environment, the recorded sessions
  (read-only) and the run folder. It cannot see the key file, the harness's environment or other
  processes. prlimit caps it at 3 GB of memory, 200 MB per file and 130 s of CPU. With `--public`, the
  harness refuses to run the agent's code if bubblewrap is missing.
- Runs are written to a 5 GB volume of their own, so a runaway analysis can't fill the system disk.
- Both services run as the unprivileged `aisim` user with a read-only system (`ProtectSystem=strict`).

What remains possible: anyone can spend up to the daily cap, and what a visitor's agent writes shows on
the dashboard for others watching. For a hard ceiling, give the deployment its own API key in a Claude
Console workspace with a monthly spend limit.

## Costs

e2-small with a 20 GB disk and a static address: about $15 a month while running (`deploy/gcp.sh stop`
pauses the VM charge; the address and disk still cost about $4 a month). API use as above.
