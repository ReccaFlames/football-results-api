# The home server runs single-node k3s with Flux GitOps

The first real deployment is a private instance (ADR 0008) on a home PC (Intel Celeron, 8 GB RAM,
128 GB disk) behind a residential router. The machine is meant to host the operator's other
experiments too, so we run a single-node **k3s** cluster on Ubuntu Server 24.04 rather than the
Compose stack from `docs/DEPLOYMENT.md`. Changes reach it by **pull**: **Flux** in the cluster
watches a private `homelab` repository and GHCR, so GitHub never holds credentials into the home
network. Public traffic enters only through a **Cloudflare Tunnel**; admin access (SSH, kube API,
Grafana) goes only over **Tailscale**. No router port is forwarded.

## Considered options

- **Compose on the server first, k3s later**: rejected. Every operator step (Caddy, backups,
  keys) would be built twice and the database migrated once more; the machine's purpose is a
  multi-app platform.
- **kubeadm or Talos**: rejected for now. kubeadm is heavier to run on 8 GB; Talos replaces
  Ubuntu. k3s still leaves the platform layer (ingress, secrets, storage, backups) to build by hand.
- **Push deploys from GitHub Actions over Tailscale**: rejected. It needs a secret in GitHub that
  can reach the cluster. **Argo CD** was rejected on memory (about 3x Flux).
- **Port forwarding 80/443 with cert-manager and dynamic DNS**: rejected. It exposes the home IP,
  fails behind CGNAT, and needs a DNS updater.

## Consequences

- Compose stays the local development and integration-test tool; `compose.yaml` is not used on the
  home server.
- This repository ships a Kustomize base in `deploy/k8s/` without secrets; the `homelab`
  repository holds the cluster, the SOPS-encrypted secrets, and the image tag that Flux bumps.
- Every green push to `main` deploys (images tagged `main-<run_number>-<sha>`). `v*` tags stay
  reserved for the manual release workflow (ADR 0009).
- One replica each of `api`, `scheduler`, and `web` with the `Recreate` strategy, because
  `docker-entrypoint.sh` runs `alembic upgrade head` and the scheduler must be single. A deploy
  costs a few seconds of downtime.
- The keys file lives on a writable volume, read-only in `api` and writable in a `toolbox`
  Deployment, because `keys` and `access-requests approve` edit it at runtime.
- `FOOTBALL_RESULTS_TRUSTED_PROXIES` is the k3s pod network (`10.42.0.0/16`), not the Compose
  range.
- Kubelet rotates logs by size, not age, so a host timer enforces the log retention `/privacy`
  promises.
