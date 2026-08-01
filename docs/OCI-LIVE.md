# Live OCI bus (`iac-bus-6c58`)

Provisioned 2026-08-01 from Cursor Cloud.

| Field | Value |
|-------|--------|
| OCI name | `iac-bus-6c58` |
| Public IP | `129.153.192.75` |
| Private IP | `10.0.1.10` |
| Shape | VM.Standard.A1.Flex 1 OCPU / 6GB |
| Port | **8101** (8091 reserved for osl-oc-agent chat on the KSG stack host) |
| Install | `/opt/iac-bus` + `/etc/iac-bus/iac-bus.env` |
| Branch deployed | `cursor/iac-bus-skill-oci-6c58` |
| SSH | `ssh -i ~/.ssh/oci_console ubuntu@129.153.192.75` |

## Reachability

- Host listens on `0.0.0.0:8101` with bearer token (`BUS_API_TOKEN`).
- Public `:8101` is open on the VCN security list + host iptables.
- Prefer public URL from outside the VCN; private `10.0.1.10` may still hit host REJECT
  from some peers — public IP works from ksg-main.

```bash
export IAC_BUS_URL=http://129.153.192.75:8101
export IAC_BUS_TOKEN="$(ssh -i ~/.ssh/oci_console ubuntu@129.153.192.75 \
  "sudo grep ^BUS_API_TOKEN= /etc/iac-bus/iac-bus.env | cut -d= -f2")"
./scripts/bus_smoke.sh
```

## Verified

- [x] `systemctl is-active iac-bus` → active
- [x] Public `/health` + bearer-gated `/bus/messages`
- [x] `./scripts/bus_smoke.sh` (unique queues; post/list/claim/ack/nack/session)
- [x] `osl-oc-agent` full claim/ack demo from prod host
- [x] Prod chat auto-announce: `osl-oc-agent-prod|done|[completed] … bus-engage-ok`

## Redeploy

```bash
# On the VM (public GitHub clone)
cd ~/iac-bus
git fetch origin && git checkout cursor/iac-bus-skill-oci-6c58
git pull --ff-only
sudo env BUS_PORT=8101 ./deploy.sh
# preserve token in /etc/iac-bus/iac-bus.env, then:
sudo systemctl restart iac-bus
```
