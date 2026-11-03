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

## Coordination dev VM (measured 2026-11-03)

A second VM runs the coordination branch (locks, MCP adapter, run tokens) so
the production bus above stays untouched.

| Field | Value |
| --- | --- |
| Instance | `iac-bus-coord-dev` (A1.Flex, 1 OCPU, 6 GB, Ubuntu 22.04 aarch64, same subnet as `iac-bus-6c58`) |
| Public / private IP | `129.146.71.34` / `10.0.1.140` (relaunched 2026-11-03 03:13 UTC with Phase 4) |
| Code | branch `claude/iac-bus-coordination-phases-vhy9iw` at `aeb329a` (locks, MCP, run tokens, A2A), `/opt/iac-bus`, `iac-bus.service` |
| Bus | `http://129.146.71.34:8101`, token required (`/health` and `/.well-known/agent-card.json` open) |
| Deployed by | OCI API only: `scripts/oci_launch_bus_vm.py` (cloud-init clones, installs, opens iptables, starts systemd) |
| Tags | `created-by=oci_launch_bus_vm.py`, `branch=claude/iac-bus-coordination-phases-vhy9iw` |

Facts found while deploying:

- The production VM `iac-bus-6c58` still serves an old build (`/health` returns
  only `messages` and `status`). It has no Run Command plugin, and the only
  Run Command IAM policy covers a different instance, so it cannot be updated
  through the OCI API without its SSH login key or IAM changes.
- The bus token for `iac-bus-coord-dev` is in its cloud-init `user_data`
  (instance metadata). To read it without printing it to a shared log:
  `oci compute instance get --instance-id <id> --query 'data.metadata."user_data"' --raw-output | base64 -d | grep BUS_API_TOKEN`.
- Plain HTTP on `:8101` sends the token in clear text. The Claude Code cloud
  egress proxy refuses to forward an `Authorization` header to a non-TLS port,
  so cloud agents cannot use this bus until it has HTTPS. Plan:
  `scripts/oci_launch_bus_vm.py --https` (Caddy + Let's Encrypt on
  `<ip-with-dashes>.sslip.io`, bus bound to localhost), which needs ingress on
  80 and 443 for this VM (an NSG passed with `--nsg-id`). Opening those ports is
  an owner decision; the script never changes network rules.

### Turning on HTTPS for the coordination VM

Cloud agents need HTTPS before they can send a bearer token to the bus. The
launch script does the VM side (Caddy, a Let's Encrypt certificate for
`<ip-with-dashes>.sslip.io`, bus bound to localhost, `BUS_PUBLIC_URL` for the
A2A card). Inbound 80 and 443 must be allowed first; this is an owner step
because it opens ports to the internet:

```bash
# 1. A network security group in waydroid-vcn that allows only 80 and 443 (attach it to the bus VM only)
oci network nsg create --compartment-id <compartment-ocid> --vcn-id <waydroid-vcn-ocid> \\
  --display-name iac-bus-coord-dev-https
oci network nsg rules add --nsg-id <nsg-ocid> --security-rules '[
  {"direction":"INGRESS","protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK",
   "tcpOptions":{"destinationPortRange":{"min":80,"max":80}}},
  {"direction":"INGRESS","protocol":"6","source":"0.0.0.0/0","sourceType":"CIDR_BLOCK",
   "tcpOptions":{"destinationPortRange":{"min":443,"max":443}}}]'

# 2. A fresh VM with HTTPS, then retire the plain-HTTP one
python scripts/oci_launch_bus_vm.py --branch claude/iac-bus-coordination-phases-vhy9iw \\
  --name iac-bus-coord-dev-https --https --nsg-id <nsg-ocid>
# prints BUS_URL=https://<ip-with-dashes>.sslip.io; the token is in .oci-vm/iac-bus-coord-dev-https/bus_token

# 3. Real Claude Code sessions against it
BUS_URL=https://<ip-with-dashes>.sslip.io BUS_API_TOKEN="$(cat .oci-vm/iac-bus-coord-dev-https/bus_token)" \\
  python scripts/claude_agents_trial.py --out docs/CLAUDE_TRIAL_OCI.md
```

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
