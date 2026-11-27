# Home server plan (k3s)

Plan for running a [private instance](PRIVATE.md) on a home server: a single-node k3s cluster that
also hosts the operator's other experiments. Decided 2026-11-27 in
[ADR 0011](adr/0011-home-server-runs-single-node-k3s-with-flux.md); tracked as roadmap item 22.
The Compose path in [DEPLOYMENT.md](DEPLOYMENT.md) (Oracle VM, Caddy) stays valid for other
deployments; the provider rules there and in [PRIVATE.md](PRIVATE.md) apply here unchanged.

Nothing is published by this plan: no PyPI release, no MCP Registry entry, no `v*` tag. Only
container images go to GHCR, because the cluster pulls them from there.

## Decisions

| Area | Decision |
| --- | --- |
| Instance | One private instance (`FOOTBALL_RESULTS_ANONYMOUS_ACCESS=false`), full `config/competitions.yaml`. No demo instance for now |
| Hardware | Home PC: Intel Celeron (amd64), 8 GB RAM, 128 GB disk, behind the home router |
| OS | Ubuntu Server 24.04 LTS, headless, no full-disk encryption; unattended-upgrades, `ufw` deny inbound |
| Cluster | k3s, single node, pinned version; bundled Traefik, CoreDNS, local-path storage, network policies |
| Host setup | Ansible playbook in the private `homelab` repository, run from the Mac |
| Admin access | Tailscale only: SSH and the kube API on the host's tailnet address; admin UIs through the Tailscale Kubernetes operator |
| Public access | Cloudflare Tunnel (`cloudflared` in the cluster) to Traefik; no router port forwarding. Domain with Cloudflare DNS (free plan) |
| Hostnames | One public subdomain per experiment, this one `football.<domain>`; admin tools never public |
| Delivery | Public GitHub repository; CI builds `api` and `web` images to public GHCR on every green push to `main`; Flux (GitOps) pulls them |
| Manifests | This repository: Kustomize base in `deploy/k8s/` (no secrets). `homelab` repository: cluster, platform, per-app overlays, secrets |
| Secrets | SOPS with age in the `homelab` repository; the age key only on the server and in the operator's password manager |
| Database | CloudNativePG operator; base backups and WAL archive to Backblaze B2 (Barman Cloud plugin) |
| Other backups | restic to B2 for the keys-file volume |
| Monitoring | kube-prometheus-stack without Alertmanager; the repository's Grafana dashboard and alert rules (Telegram); external uptime check |
| Isolation | Per experiment: namespace, default-deny NetworkPolicy, Pod Security `restricted`, ResourceQuota and LimitRange |
| Environments | Production only. Compose on the Mac stays the development and integration-test setup |
| Data | Start empty; the scheduler fills current seasons, past seasons are imported with the admin refresh job |
| Out of scope | OAuth for `/mcp` (roadmap item 20), demo instance, publishing (item 15), multi-node, log aggregation |

## Architecture

```text
                    Internet                                   Tailnet (admin only)
                       │                                              │
             Cloudflare edge (TLS, DNS)                      Mac: ssh, kubectl, flux,
                       │ tunnel (outbound only)               Grafana in the browser
┌──────────────────────┼───────────────────── home server ────────────┼──────────────────┐
│ Ubuntu 24.04 · ufw (deny inbound) · tailscaled · k3s                │                  │
│                      ▼                                              ▼                  │
│  cloudflared ──▶ Traefik (kube-system) ──┬─▶ web  ──▶ api    Tailscale operator ─▶ Grafana
│   (ns cloudflared)                       └─▶ api                                       │
│                                                                                        │
│  ns football: api · scheduler · web · toolbox · CNPG cluster `football-db` · PVC keys │
│  ns flux-system · cnpg-system · monitoring · tailscale · (later: one ns per experiment) │
└────────────────────────────────────────────────────────────────────────────────────────┘
         CNPG WAL + base backups, restic snapshots ──▶ Backblaze B2 (encrypted)
```

### Routes

Traefik replaces Caddy and carries the same path routes (an `Ingress` or `IngressRoute` in
`deploy/k8s/`):

| Path on `football.<domain>` | Service |
| --- | --- |
| `/`, `/request-access`, `/static/*`, `/favicon.ico` | `web:8001` |
| Everything else | `api:8000` |

Keep Traefik access logs off: they would hold client IPs outside the retention promised on
`/privacy`.

### Client addresses and trusted proxies

```text
visitor ─▶ Cloudflare edge ─▶ cloudflared pod ─▶ Traefik pod ─┬─▶ api
                                                              └─▶ web pod ─▶ api
```

Cloudflare puts the visitor's address in `X-Forwarded-For`. All three in-cluster hops have pod
addresses in the k3s cluster CIDR, so:

- `FOOTBALL_RESULTS_TRUSTED_PROXIES=10.42.0.0/16` (k3s default `--cluster-cidr`; check it in the
  k3s config). The API then picks the rightmost untrusted address: the visitor.
- Traefik must keep incoming forwarded headers from cloudflared: set
  `entryPoints.web.forwardedHeaders.trustedIPs` to the same range through a `HelmChartConfig`.
- **Verify** after the first deploy, from outside the home network: two different clients get
  separate rate-limit counters (`RateLimit` headers), and `POST /request-access` counts per
  visitor, not per pod.

## Resource budget

Planning numbers for 8 GB RAM; set them as requests and limits, then measure with Grafana and adjust.

| Component | Memory (limit) |
| --- | --- |
| Ubuntu, tailscaled, k3s server, containerd, CoreDNS, Traefik, metrics-server | ~1.3 GB |
| Flux (source, kustomize, helm, notification, image reflector and automation) | ~0.4 GB |
| cloudflared, Tailscale operator and proxy | ~0.2 GB |
| CloudNativePG operator + `football-db` (one instance, `shared_buffers` 256 MB) | ~1.1 GB |
| `api` 384 Mi, `scheduler` 256 Mi, `web` 128 Mi, `toolbox` 128 Mi | ~0.9 GB |
| Prometheus (retention 15 d, `retentionSize` 8 GB), Grafana, node-exporter, kube-state-metrics, operator | ~1.5 GB |
| **Headroom for experiments** | **~2.5 GB** |

Disk (128 GB): about 15 GB OS, 20 GB container images (kubelet image GC thresholds), 10 GB
PostgreSQL with local WAL, 8 GB Prometheus, the rest for experiments. Set the `football` namespace
quota around 1 CPU / 2 GiB requests and 3 GiB limits.

## Phases

Each phase ends in a state you can check. Roadmap item 22 tracks them as sub-tasks.

### 0. GitHub and images (this repository)

1. Create the public repository and push `main`:
   `gh repo create ReccaFlames/football-results-api --public --source . --push`. Confirm the first
   CI run is green. Protect `main` (require the CI check). Do not push a `v*` tag: it runs
   `release.yml` (ADR 0009).
2. Add an image workflow: on push to `main`, call `ci.yml` (`workflow_call`), then build
   `Dockerfile` and `web/Dockerfile` for `linux/amd64` and push
   `ghcr.io/reccaflames/football-results-api` and `ghcr.io/reccaflames/football-results-web`
   tagged `main-<run_number>-<short sha>` (sortable for Flux), with `packages: write` only in that
   job. Make both GHCR packages public.
3. Add the `deploy/k8s/` Kustomize base (see [The app base](#the-app-base)) and a CI step that
   renders it and validates it with `kubeconform -strict` (CRD schemas for CloudNativePG,
   Traefik, and Prometheus Operator from a catalog). Not in pytest: tests stay offline.

### 1. Host (the `homelab` repository, Ansible)

1. Install Ubuntu Server 24.04 LTS with OpenSSH, a non-root admin user, and your Mac's SSH key.
   Reserve the server's address in the router (DHCP lease).
2. Ansible playbook, first run over the LAN, later runs over the tailnet:
   - `unattended-upgrades` with automatic reboots in a night window; `needrestart`.
   - SSH: keys only, no root login, `ListenAddress` on the tailnet address once Tailscale is up.
   - Tailscale (`tailscale up --ssh=false`, key expiry disabled for the server node, ACL: only
     your devices may reach it).
   - `ufw`: deny inbound, allow `tailscale0`; allow the k3s pod and service networks
     (`10.42.0.0/16`, `10.43.0.0/16`) so in-cluster traffic is not dropped.
   - k3s, pinned version, `--tls-san` with the tailnet name and address, `--write-kubeconfig-mode
     600`, `--secrets-encryption`, kubelet `container-log-max-size=10Mi` and
     `container-log-max-files=3`.
   - The log retention timer ([Logs and privacy](#logs-and-privacy)).
   - Directory for the age key (`/root/.config/sops/age/keys.txt`, mode 600), filled by hand.
3. Copy the kubeconfig to the Mac with the server URL set to the tailnet name. Check:
   `kubectl get nodes` from the Mac with Tailscale on, and that it fails with Tailscale off.

### 2. Platform (the `homelab` repository, Flux)

1. Create the private `homelab` repository and run `flux bootstrap github` with a read-write
   deploy key (image automation commits tag bumps). Generate the age key, add its public part to
   `.sops.yaml`, and give Flux the `sops-age` secret.
2. Cluster add-ons, each a Flux `HelmRelease` or `Kustomization`, in dependency order:
   - Traefik `HelmChartConfig`: forwarded-header trusted IPs, access logs off.
   - `cloudflared` with a remotely managed tunnel token (SOPS). In Cloudflare: public hostname
     `football.<domain>` → `http://traefik.kube-system.svc.cluster.local:80`, Always Use HTTPS.
   - Tailscale Kubernetes operator (OAuth client in SOPS); Grafana exposed as a tailnet device
     with HTTPS.
   - CloudNativePG operator and the Barman Cloud plugin; B2 bucket and application key (SOPS).
   - kube-prometheus-stack: Alertmanager off, Grafana with the sidecar loading dashboards and
     alert rules from labelled ConfigMaps, admin password in SOPS. The Telegram contact point is
     a provisioning file stored as a Secret (written as text, never interpolated, because Grafana
     turns numeric-looking values into numbers).
   - restic backup CronJob template (B2 repository and password in SOPS).
3. A namespace template for experiments: `Namespace` with Pod Security `restricted` labels,
   default-deny `NetworkPolicy` (ingress and egress) plus DNS egress, `ResourceQuota`,
   `LimitRange`.
4. Check: `flux get all -A` is ready; Grafana opens over the tailnet; the cluster dashboard shows
   the node.

### 3. The app (both repositories)

1. In `homelab`, `apps/football/`: the namespace from the template, a Flux `GitRepository` for
   this repository (branch `main`, path `deploy/k8s`), an overlay with the hostname, Flux
   `ImageRepository`, `ImagePolicy` (`^main-(?P<n>[0-9]+)-[0-9a-f]+$`, numeric on `n`), and
   `ImageUpdateAutomation`, and SOPS secrets: football-data.org token, TheSportsDB key, Telegram
   bot token and chat ID, Prometheus admin key, B2 credentials.
2. Settings that differ from the Compose `.env` in [PRIVATE.md](PRIVATE.md#1-settings):
   - `FOOTBALL_RESULTS_DATABASE_URL` built from the CloudNativePG app secret
     (`postgresql+psycopg_async://$(DB_USER):$(DB_PASSWORD)@football-db-rw:5432/football`; check
     the generated password is URL-safe and passes `weak_database_password`).
   - `FOOTBALL_RESULTS_PUBLIC_BASE_URL=https://football.<domain>`.
   - `FOOTBALL_RESULTS_TRUSTED_PROXIES=10.42.0.0/16`.
   - `FOOTBALL_RESULTS_API_KEYS_FILE=/app/secrets/api-keys.yaml` on the keys volume.
   - `FOOTBALL_RESULTS_ANONYMIZE_LOG_IPS=true`, `FOOTBALL_RESULTS_LOG_RETENTION_DAYS=14`.
   - `FOOTBALL_RESULTS_WEB_API_URL=http://api:8000`, `FOOTBALL_RESULTS_WEB_PUBLIC_API_URL=`
     (empty: one origin, relative links).
   - `FOOTBALL_RESULTS_USER_AGENT` and `FOOTBALL_RESULTS_OPERATOR_CONTACT`.
3. First start: a private instance refuses to start without the keys file. Let the `toolbox`
   create it first (phase 4, admin key for Prometheus); `api` crash-loops until then, which is
   expected once.
4. Check from the Mac:
   `kubectl -n football exec deploy/toolbox -- python -m app.config check`; then from outside the
   home network: the landing page with the form, `401` without a key, `/health` with
   `"scheduler":"ok"`, `/privacy`, and the forwarded-address check above.

### 4. Operating keys and access requests

All `python -m app.config` commands run in the `toolbox` Deployment (the API image with
`sleep infinity`, the database URL, and the keys volume mounted read-write) over the tailnet:

```bash
kubectl -n football exec deploy/toolbox -- python -m app.config keys --file /app/secrets/api-keys.yaml issue ...
kubectl -n football exec deploy/toolbox -- python -m app.config access-requests list
kubectl -n football exec deploy/toolbox -- python -m app.config access-requests approve <id>
```

The helpers replace the file atomically in the same directory, and the API's `KeysFileWatcher`
sees the new inode on the shared volume. Both pods mount the same `ReadWriteOnce` local-path volume,
which works because there is one node. The commands that apply in [PRIVATE.md](PRIVATE.md) sections 3–5
are the same; only the prefix differs.

### 5. Backups, monitoring, go-live

1. CloudNativePG `ScheduledBackup` daily, WAL archiving continuous, retention 30 days in B2.
2. restic CronJob daily for the keys volume, with `forget --keep-daily 14 --keep-weekly 8`.
   The age key and the B2 credentials also live in the password manager: without them the backups
   cannot be read.
3. Restore test, once, before relying on it: a scratch namespace with a CloudNativePG cluster
   bootstrapped from the B2 backup, and `restic restore` of the keys file; compare row counts and
   the keys file.
4. UptimeRobot (free) on `https://football.<domain>/health`, alerting unless the body contains
   `"scheduler":"ok"`; this also catches power and internet outages, which Grafana cannot report.
5. Run the agent evals in `evals/` once against the instance (by hand, never in CI).
6. Import past seasons with the admin refresh job if wanted (DEMO.md, "Importing a past season",
   with an admin key from the toolbox).

## The app base

`deploy/k8s/` in this repository, with no secrets, no hostname, and no namespace hard-coded:

- `Deployment`s `api`, `scheduler`, `web`, `toolbox`: one replica each, `strategy: Recreate`,
  UID 10001, `runAsNonRoot`, `seccompProfile: RuntimeDefault`, all capabilities dropped, read-only
  root filesystem with an `emptyDir` on `/tmp`; resource requests and limits from the budget
  above. `api` runs `docker-entrypoint.sh` (migrations, then Uvicorn), so two API pods must never
  start together.
- `Service`s `api` and `web`; the path routes; a `PersistentVolumeClaim` `api-keys` (100 Mi).
- CloudNativePG `Cluster` `football-db` (one instance, PostgreSQL 17); backup settings come from
  the overlay.
- `NetworkPolicy`s: `api` from Traefik, `web`, and Prometheus; `web` from Traefik; the database
  from `api`, `scheduler`, `toolbox`, and the CloudNativePG operator; egress to the database,
  DNS, and (for `api` and `scheduler`) the internet on 443 for providers and Telegram.
- `ServiceMonitor` for `/metrics` with the admin key as a bearer token from a secret the overlay
  provides.
- Grafana dashboard and alert rules from `observability/grafana/` as labelled ConfigMaps. Open
  point for 22c: Kustomize (and Flux) refuse files outside the kustomization root, so either the
  base generates them from a root-level kustomization or a test keeps the copies equal. Check the
  Prometheus data source UID the rules expect.

### Rules for changes once this runs

- The manifests come from `main` directly, but images arrive a few minutes later through CI and
  Flux. Manifest changes must work with the previous image too (add a setting with a default
  first, use it later), like migrations.
- A failing migration crash-loops `api`. Roll back by reverting the commit; Alembic does not
  downgrade automatically.

## Logs and privacy

`/privacy` says access logs are kept for at most `FOOTBALL_RESULTS_LOG_RETENTION_DAYS` days.
Kubelet rotates container logs by size, not age, so:

- `FOOTBALL_RESULTS_ANONYMIZE_LOG_IPS=true`: logs never hold full addresses.
- A daily systemd timer from Ansible deletes rotated files under `/var/log/pods` older than 14
  days and truncates an active log whose oldest line is older than that. Verify that containerd
  keeps writing after truncation.
- No log shipping. Prometheus and Grafana hold metrics, not IPs. Cloudflare is covered by the
  "CDN or reverse proxy" sentence on `/privacy`.

## Later

- OAuth for chat clients (roadmap item 20).
- A demo instance in its own namespace and subdomain (`compose.demo.yaml` equivalent,
  ADR 0007).
- Publishing the CLI and MCP Registry entry (item 15, by hand with `scripts/publish-wizard.sh`).
- Renovate for the `homelab` repository's Helm charts; k3s upgrades through the playbook.
- A second node, or Loki for logs, if an experiment needs them.
