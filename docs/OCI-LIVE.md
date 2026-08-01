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
- Public `:8101` is currently **blocked/filtered** by the VCN security list from outside.
- From a Cloud Agent / laptop, tunnel:

```bash
ssh -i ~/.ssh/oci_console -N -L 18101:127.0.0.1:8101 ubuntu@129.153.192.75
export IAC_BUS_URL=http://127.0.0.1:18101
export IAC_BUS_TOKEN="$(ssh -i ~/.ssh/oci_console ubuntu@129.153.192.75 \
  "sudo grep ^BUS_API_TOKEN= /etc/iac-bus/iac-bus.env | cut -d= -f2")"
./scripts/bus_smoke.sh
```

Inside the VCN, peers can use `http://10.0.1.10:8101` once security lists allow TCP 8101 on the subnet.

## Verified

- [x] `systemctl is-active iac-bus` → active
- [x] Local `/health` on VM
- [x] Tunneled `./scripts/bus_smoke.sh` (post/list/claim/ack/nack/session)
- [x] `osl-oc-agent` `scripts/iac_bus_demo.mjs` posted `progress` to `ops`

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
