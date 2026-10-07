# OCI live edge (iac-bus)

Captain-approved public hostname: **`iac-bus.knowshowgo.com`**.

## Measured topology (2026-08-26)

| Field | Value |
| --- | --- |
| OCI region | PHX (home) |
| Instance | `iac-bus-6c58` |
| Public IP | `129.153.192.75` |
| Private IP | `10.0.1.10` |
| Bus listen (today) | **`:8101`** (health OK without auth) |
| Auth | Bearer token required for non-`/health` routes |
| DNS | `iac-bus.knowshowgo.com` **not resolving** — create A → `129.153.192.75` |
| Sibling reference | `knowshowgo.com` / `api.knowshowgo.com` → `129.153.118.145` (`ksg-main-eb91`) |

## DNS cutover checklist

1. Create A record: `iac-bus.knowshowgo.com` → `129.153.192.75` (TTL 300).
2. `dig +short iac-bus.knowshowgo.com A` returns the IP.
3. Prefer TLS:
   - Install Caddy/nginx on the VM.
   - Proxy `https://iac-bus.knowshowgo.com` → `127.0.0.1:8101` (or `:8091` if you standardize).
   - Open OCI NSG/security list for **80/443**; keep **8101** VCN-only once proxy is up.
4. Verify:
   ```bash
   curl -fsS https://iac-bus.knowshowgo.com/health
   ```

## Deploy paths

| Env | Unit | Script | CI |
| --- | --- | --- | --- |
| Dev (hot reload) | `iac-bus-dev.service` | `scripts/deploy-dev-vm.sh` | `dev-deploy.yml` on `dev` |
| Prod (stable) | `iac-bus.service` | `scripts/deploy-prod-vm.sh` | `prod-deploy.yml` on `master` / tags |

Prod script expects secrets named `IAC_BUS_PROD_HOST` (may be IP or hostname),
`IAC_BUS_PROD_USER`, `IAC_BUS_PROD_KEY`, optional `BUS_API_TOKEN`.

Until DNS exists, set `IAC_BUS_PROD_HOST=129.153.192.75`.

## SSH note

Cloud agent `OCI_SSH_PRIVATE_KEY` is typically the **OCI API** PEM, not the instance
login key. Deploy CI must use `IAC_BUS_PROD_KEY` / `KSG_DEV_VM_KEY` (VM authorized_keys).

## Security

- Never commit `BUS_API_TOKEN`.
- Rotate token after any accidental paste into chat/logs.
- KeyChain is not required for this edge.
