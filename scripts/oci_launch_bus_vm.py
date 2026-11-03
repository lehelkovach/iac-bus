#!/usr/bin/env python3
"""Launch an OCI VM that runs iac-bus from a git branch, using only the OCI API.

No SSH is needed. The VM's cloud-init clones the branch, builds a venv,
writes /etc/iac-bus/iac-bus.env (with a fresh BUS_API_TOKEN), installs the
systemd unit, opens the port in the image's iptables and starts the bus. The
script then waits for the bootstrap markers on the serial console and prints
the VM's own health check.

Placement (availability domain, subnet, image, compartment) is copied from a
reference instance, by default the existing bus VM (IAC_BUS_INSTANCE_OCID).

  python scripts/oci_launch_bus_vm.py --branch dev --name iac-bus-dev-2
  python scripts/oci_launch_bus_vm.py --branch dev --https --nsg-id ocid1.networksecuritygroup...

--https puts Caddy in front with a Let's Encrypt certificate for
<ip-with-dashes>.sslip.io and binds the bus to 127.0.0.1. Let's Encrypt must
reach the VM on ports 80 and 443, so the subnet's security list or the NSG
given with --nsg-id has to allow them. This script never changes network
security rules itself.

OCI credentials come from the environment, under either naming scheme:
  OCI_TENANCY_OCID | OCI_TENANCY_ID, OCI_USER_OCID | OCI_USER_ID,
  OCI_FINGERPRINT | OCI_SSH_FINGERPRINT, OCI_REGION | OCI_REGION_ID,
  OCI_PRIVATE_KEY | OCI_SSH_PRIVATE_KEY (PEM content; the OCI API signing key)

Secrets are never printed. The bus token and the VM's SSH key are written
with 0600 permissions to --secrets-dir (default ./.oci-vm/<name>/).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Dict, List

try:
    import oci
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Needs: pip install oci cryptography") from exc

REPO_URL = "https://github.com/lehelkovach/iac-bus.git"


def _env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise SystemExit(f"Missing environment variable: one of {', '.join(names)}")


def oci_config() -> Dict[str, str]:
    cfg = {
        "tenancy": _env("OCI_TENANCY_OCID", "OCI_TENANCY_ID"),
        "user": _env("OCI_USER_OCID", "OCI_USER_ID"),
        "fingerprint": _env("OCI_FINGERPRINT", "OCI_SSH_FINGERPRINT"),
        "region": _env("OCI_REGION", "OCI_REGION_ID"),
        "key_content": _env("OCI_PRIVATE_KEY", "OCI_SSH_PRIVATE_KEY").replace("\\n", "\n"),
    }
    oci.config.validate_config(cfg)
    return cfg


def cloud_init(branch: str, port: int, token: str, https: bool) -> str:
    bind = "127.0.0.1" if https else "0.0.0.0"
    caddy = ""
    if https:
        caddy = r"""
apt-get install -y debian-keyring debian-archive-keyring apt-transport-https gnupg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -y && apt-get install -y caddy
PUBLIC_IP=$(curl -fsS -m 10 https://api.ipify.org || curl -fsS -m 10 https://ifconfig.me)
HOST="$(echo "$PUBLIC_IP" | tr . -).sslip.io"
cat > /etc/caddy/Caddyfile <<CADDY
$HOST {
    reverse_proxy 127.0.0.1:__PORT__
}
CADDY
iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT
netfilter-persistent save
systemctl restart caddy
for i in $(seq 1 60); do curl -fsS "https://$HOST/health" >/dev/null && break; sleep 3; done
say "https https://$HOST $(curl -sS -m 5 https://$HOST/health || echo NOT-YET)"
""".replace("__PORT__", str(port))
    open_port = "" if https else f"iptables -I INPUT 1 -p tcp --dport {port} -j ACCEPT\nnetfilter-persistent save\n"
    return f"""#!/bin/bash
set -euxo pipefail
exec > >(tee /var/log/iac-bus-bootstrap.log) 2>&1
say() {{ echo "IAC_BUS_BOOTSTRAP $*" > /dev/console; }}
say "start $(date -u +%FT%TZ)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y python3-venv python3-pip git curl netfilter-persistent
APP=/opt/iac-bus
git clone --branch {branch} --depth 1 {REPO_URL} $APP
SHA=$(git -C $APP rev-parse HEAD)
say "git $SHA"
python3 -m venv $APP/venv
$APP/venv/bin/pip install --upgrade pip
$APP/venv/bin/pip install -r $APP/requirements.txt
mkdir -p /etc/iac-bus
umask 077
cat > /etc/iac-bus/iac-bus.env <<ENVFILE
BUS_HOST={bind}
BUS_PORT={port}
BUS_LOG_LEVEL=INFO
BUS_MAX_MESSAGES=2000
BUS_RETENTION_SECONDS=7200
BUS_QUEUE_LEASE_SECONDS=60
BUS_VERSION=$(cat $APP/VERSION)
BUS_GIT_SHA=$SHA
BUS_API_TOKEN={token}
ENVFILE
umask 022
install -m 0644 $APP/systemd/iac-bus.service /etc/systemd/system/iac-bus.service
{open_port}systemctl daemon-reload
systemctl enable --now iac-bus.service
for i in $(seq 1 30); do curl -fsS http://127.0.0.1:{port}/health && break; sleep 1; done
say "health $(curl -sS -m 5 http://127.0.0.1:{port}/health || echo FAIL)"
{caddy}
say "done $(date -u +%FT%TZ)"
"""


def console_markers(compute: "oci.core.ComputeClient", instance_id: str) -> List[str]:
    hist = compute.capture_console_history(oci.core.models.CaptureConsoleHistoryDetails(instance_id=instance_id)).data
    for _ in range(20):
        if compute.get_console_history(hist.id).data.lifecycle_state == "SUCCEEDED":
            break
        time.sleep(3)
    text = compute.get_console_history_content(hist.id, length=1048576).data
    text = text if isinstance(text, str) else text.decode(errors="replace")
    return [line.strip() for line in text.splitlines() if line.startswith("IAC_BUS_BOOTSTRAP")]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--branch", default="dev")
    ap.add_argument("--name", default="iac-bus-dev")
    ap.add_argument("--ref-instance", default=os.environ.get("IAC_BUS_INSTANCE_OCID", ""),
                    help="instance to copy AD, subnet, image and compartment from")
    ap.add_argument("--shape", default="VM.Standard.A1.Flex")
    ap.add_argument("--ocpus", type=float, default=1.0)
    ap.add_argument("--memory-gbs", type=float, default=6.0)
    ap.add_argument("--port", type=int, default=8101)
    ap.add_argument("--https", action="store_true", help="Caddy + Let's Encrypt on <ip>.sslip.io; needs 80/443 ingress")
    ap.add_argument("--nsg-id", action="append", default=[], help="NSG(s) to attach to the VNIC")
    ap.add_argument("--secrets-dir", default="")
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for the bootstrap to finish")
    args = ap.parse_args()
    if not args.ref_instance:
        raise SystemExit("--ref-instance (or IAC_BUS_INSTANCE_OCID) is required")

    cfg = oci_config()
    compute = oci.core.ComputeClient(cfg)
    net = oci.core.VirtualNetworkClient(cfg)
    ref = compute.get_instance(args.ref_instance).data
    att = compute.list_vnic_attachments(compartment_id=ref.compartment_id, instance_id=ref.id).data[0]
    subnet_id = net.get_vnic(att.vnic_id).data.subnet_id

    out = Path(args.secrets_dir or f".oci-vm/{args.name}")
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    token = secrets.token_urlsafe(32)
    key = ed25519.Ed25519PrivateKey.generate()
    for fname, data in (("bus_token", token.encode()),
                        ("ssh_key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                                      serialization.NoEncryption()))):
        (out / fname).write_bytes(data)
        os.chmod(out / fname, 0o600)
    pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()

    details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ref.availability_domain, compartment_id=ref.compartment_id, display_name=args.name,
        shape=args.shape,
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=args.ocpus, memory_in_gbs=args.memory_gbs)
        if args.shape.endswith(".Flex") else None,
        source_details=oci.core.models.InstanceSourceViaImageDetails(image_id=ref.source_details.image_id),
        create_vnic_details=oci.core.models.CreateVnicDetails(subnet_id=subnet_id, assign_public_ip=True,
                                                              nsg_ids=args.nsg_id or None),
        metadata={"ssh_authorized_keys": f"{pub} {args.name}",
                  "user_data": base64.b64encode(cloud_init(args.branch, args.port, token, args.https).encode()).decode()},
        freeform_tags={"created-by": "oci_launch_bus_vm.py", "branch": args.branch},
    )
    inst = compute.launch_instance(details).data
    print(f"launched {args.name}; waiting for RUNNING", flush=True)
    inst = oci.wait_until(compute, compute.get_instance(inst.id), "lifecycle_state", "RUNNING", max_wait_seconds=600).data
    vnic = net.get_vnic(compute.list_vnic_attachments(compartment_id=inst.compartment_id, instance_id=inst.id).data[0].vnic_id).data
    info = {"instance_id": inst.id, "name": args.name, "public_ip": vnic.public_ip, "private_ip": vnic.private_ip,
            "branch": args.branch, "port": args.port, "https": args.https}
    (out / "instance.json").write_text(json.dumps(info, indent=2))
    print(f"running: public {vnic.public_ip}, private {vnic.private_ip}; waiting for bootstrap", flush=True)

    deadline = time.time() + args.wait
    markers: List[str] = []
    while time.time() < deadline:
        markers = console_markers(compute, inst.id)
        if any(re.search(r" done ", m) for m in markers):
            break
        time.sleep(30)
    for marker in markers:
        print(marker.replace(token, "<token>"))
    url = f"http://{vnic.public_ip}:{args.port}"
    for marker in markers:
        if marker.startswith("IAC_BUS_BOOTSTRAP https "):
            url = marker.split()[2]
    print(f"\nBUS_URL={url}\nBUS_API_TOKEN: in {out / 'bus_token'} (0600)\nSSH key: {out / 'ssh_key'} (user ubuntu)")
    return 0 if any(" done " in m for m in markers) else 1


if __name__ == "__main__":
    sys.exit(main())
