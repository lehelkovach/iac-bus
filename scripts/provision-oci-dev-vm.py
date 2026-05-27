#!/usr/bin/env python3
"""
Provision a new Oracle Cloud VM for IAC Bus development using OCI_* secrets.

Required env vars:
  OCI_TENANCY_OCID
  OCI_USER_OCID
  OCI_FINGERPRINT
  OCI_REGION
  OCI_COMPARTMENT_OCID
  OCI_SUBNET_OCID
  OCI_IMAGE_OCID
  OCI_SSH_PUBLIC_KEY

Private key input (one of):
  OCI_PRIVATE_KEY           (raw key content OR local file path)
  OCI_PRIVATE_KEY_B64       (base64-encoded key content)

Optional:
  OCI_AVAILABILITY_DOMAIN   (if unset, first AD is used)
  OCI_SHAPE                 (default: VM.Standard.E2.1.Micro)
  OCI_VM_DISPLAY_NAME       (default: iac-bus-dev-vm)
  OCI_ASSIGN_PUBLIC_IP      (default: true)
  OCI_BOOT_VOLUME_SIZE_GBS  (optional int)
  OCI_OUTPUT_ENV_FILE       (write export-ready output file)
  OCI_RUN_DEPLOY_AFTER_CREATE (true/false; default false)
  KSG_DEV_VM_USER           (default ubuntu, used for optional deploy step)
  KSG_DEV_VM_APP_DIR        (default /opt/iac-bus-dev, used for optional deploy step)
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

try:
    import oci
except ImportError as exc:  # pragma: no cover - runtime dependency check
    raise SystemExit(
        "Missing python package 'oci'. Install with: python3 -m pip install oci"
    ) from exc


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if value is None:
        return None
    return value.strip() if isinstance(value, str) else value


def _require(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_private_key() -> str:
    key_raw = _env("OCI_PRIVATE_KEY")
    key_b64 = _env("OCI_PRIVATE_KEY_B64")

    if key_raw:
        possible_path = Path(key_raw).expanduser()
        if possible_path.is_file():
            return possible_path.read_text(encoding="utf-8")
        if "\\n" in key_raw:
            return key_raw.replace("\\n", "\n")
        return key_raw

    if key_b64:
        try:
            return base64.b64decode(key_b64).decode("utf-8")
        except Exception as exc:  # pragma: no cover - invalid secret content
            raise SystemExit("Failed to decode OCI_PRIVATE_KEY_B64") from exc

    raise SystemExit("Missing private key: set OCI_PRIVATE_KEY or OCI_PRIVATE_KEY_B64")


def _build_config() -> Dict[str, str]:
    return {
        "tenancy": _require("OCI_TENANCY_OCID"),
        "user": _require("OCI_USER_OCID"),
        "fingerprint": _require("OCI_FINGERPRINT"),
        "region": _require("OCI_REGION"),
        "key_content": _resolve_private_key(),
    }


def _first_ad(identity_client: "oci.identity.IdentityClient", tenancy_id: str) -> str:
    ads = identity_client.list_availability_domains(compartment_id=tenancy_id).data
    if not ads:
        raise SystemExit("No availability domains found for tenancy")
    return ads[0].name


def _wait_for_public_ip(
    compute_client: "oci.core.ComputeClient",
    network_client: "oci.core.VirtualNetworkClient",
    compartment_id: str,
    instance_id: str,
    timeout_seconds: int = 300,
) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        attachments = compute_client.list_vnic_attachments(
            compartment_id=compartment_id, instance_id=instance_id
        ).data
        if attachments:
            vnic = network_client.get_vnic(attachments[0].vnic_id).data
            if vnic.public_ip:
                return vnic.public_ip
        time.sleep(5)
    raise SystemExit("Timed out waiting for VM public IP")


def _run_deploy_after_create(public_ip: str) -> None:
    run_flag = _parse_bool(_env("OCI_RUN_DEPLOY_AFTER_CREATE"), default=False)
    if not run_flag:
        return

    repo_root = Path(__file__).resolve().parents[1]
    deploy_script = repo_root / "scripts" / "deploy-dev-vm.sh"
    if not deploy_script.exists():
        raise SystemExit("deploy-dev-vm.sh not found for post-provision deploy")

    env = os.environ.copy()
    env["KSG_DEV_VM_HOST"] = public_ip
    env.setdefault("KSG_DEV_VM_USER", "ubuntu")
    env.setdefault("KSG_DEV_VM_APP_DIR", "/opt/iac-bus-dev")
    env.setdefault("KSG_DEV_VM_PORT", "22")
    if not env.get("KSG_DEV_VM_KEY"):
        if env.get("OCI_PRIVATE_KEY"):
            env["KSG_DEV_VM_KEY"] = env["OCI_PRIVATE_KEY"]
        elif env.get("OCI_PRIVATE_KEY_B64"):
            env["KSG_DEV_VM_KEY"] = env["OCI_PRIVATE_KEY_B64"]

    subprocess.run([str(deploy_script)], cwd=str(repo_root), env=env, check=True)


def _write_output_env(output: Dict[str, str]) -> None:
    output_file = _env("OCI_OUTPUT_ENV_FILE")
    if not output_file:
        return
    target = Path(output_file).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for key, value in output.items():
            fh.write(f"export {key}={json.dumps(value)}\n")


def main() -> int:
    config = _build_config()
    compartment_id = _require("OCI_COMPARTMENT_OCID")
    subnet_id = _require("OCI_SUBNET_OCID")
    image_id = _require("OCI_IMAGE_OCID")
    ssh_public_key = _require("OCI_SSH_PUBLIC_KEY")

    shape = _env("OCI_SHAPE", "VM.Standard.E2.1.Micro")
    display_name = _env("OCI_VM_DISPLAY_NAME", "iac-bus-dev-vm")
    assign_public_ip = _parse_bool(_env("OCI_ASSIGN_PUBLIC_IP"), default=True)

    identity_client = oci.identity.IdentityClient(config)
    compute_client = oci.core.ComputeClient(config)
    network_client = oci.core.VirtualNetworkClient(config)
    compute_composite = oci.core.ComputeClientCompositeOperations(compute_client)

    ad = _env("OCI_AVAILABILITY_DOMAIN") or _first_ad(identity_client, config["tenancy"])

    source_details = oci.core.models.InstanceSourceViaImageDetails(image_id=image_id)
    create_vnic = oci.core.models.CreateVnicDetails(
        subnet_id=subnet_id, assign_public_ip=assign_public_ip
    )

    launch_kwargs: Dict[str, object] = {
        "availability_domain": ad,
        "compartment_id": compartment_id,
        "shape": shape,
        "display_name": display_name,
        "source_details": source_details,
        "create_vnic_details": create_vnic,
        "metadata": {"ssh_authorized_keys": ssh_public_key},
    }

    boot_size = _env("OCI_BOOT_VOLUME_SIZE_GBS")
    if boot_size:
        launch_kwargs["shape_config"] = None  # keep explicit, even if default
        launch_kwargs["source_details"] = oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=int(boot_size),
        )

    details = oci.core.models.LaunchInstanceDetails(**launch_kwargs)
    response = compute_composite.launch_instance_and_wait_for_state(
        details, wait_for_states=[oci.core.models.Instance.LIFECYCLE_STATE_RUNNING]
    )
    instance = response.data

    public_ip = _wait_for_public_ip(
        compute_client=compute_client,
        network_client=network_client,
        compartment_id=compartment_id,
        instance_id=instance.id,
    )

    output = {
        "KSG_DEV_VM_HOST": public_ip,
        "KSG_DEV_VM_USER": _env("KSG_DEV_VM_USER", "ubuntu"),
        "KSG_DEV_VM_PORT": _env("KSG_DEV_VM_PORT", "22"),
        "KSG_DEV_VM_APP_DIR": _env("KSG_DEV_VM_APP_DIR", "/opt/iac-bus-dev"),
        "OCI_INSTANCE_ID": instance.id,
        "OCI_INSTANCE_AD": ad,
        "OCI_INSTANCE_SHAPE": shape,
        "OCI_INSTANCE_DISPLAY_NAME": display_name or "iac-bus-dev-vm",
    }

    _write_output_env(output)

    print(json.dumps(output, indent=2))
    _run_deploy_after_create(public_ip)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
