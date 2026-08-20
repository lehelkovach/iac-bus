import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "provision-oci-dev-vm.py"


def _load():
    spec = importlib.util.spec_from_file_location("provision_oci_dev_vm", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_first_env_prefers_ocid_spelling(monkeypatch):
    mod = _load()
    monkeypatch.setenv("OCI_TENANCY_OCID", "ocid1.tenancy.oc1..ocid")
    monkeypatch.setenv("OCI_TENANCY_ID", "ocid1.tenancy.oc1..id")
    assert mod._first_env("OCI_TENANCY_OCID", "OCI_TENANCY_ID") == "ocid1.tenancy.oc1..ocid"


def test_require_accepts_cursor_id_alias(monkeypatch):
    mod = _load()
    monkeypatch.delenv("OCI_USER_OCID", raising=False)
    monkeypatch.setenv("OCI_USER_ID", "  ocid1.user.oc1..id  ")
    assert mod._require("OCI_USER_OCID", "OCI_USER_ID") == "ocid1.user.oc1..id"


def test_build_config_from_cursor_secret_names(monkeypatch):
    mod = _load()
    for name in (
        "OCI_TENANCY_OCID",
        "OCI_USER_OCID",
        "OCI_COMPARTMENT_OCID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OCI_TENANCY_ID", "ocid1.tenancy.oc1..id")
    monkeypatch.setenv("OCI_USER_ID", "ocid1.user.oc1..id")
    monkeypatch.setenv("OCI_FINGERPRINT", "ab:cd")
    monkeypatch.setenv("OCI_REGION", "us-test-1")
    monkeypatch.setenv(
        "OCI_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----",
    )
    config = mod._build_config()
    assert config["tenancy"] == "ocid1.tenancy.oc1..id"
    assert config["user"] == "ocid1.user.oc1..id"
    assert config["fingerprint"] == "ab:cd"
    assert config["region"] == "us-test-1"
    assert "BEGIN PRIVATE KEY" in config["key_content"]
    assert "\\n" not in config["key_content"]


def test_require_missing_names_mentions_aliases(monkeypatch):
    mod = _load()
    monkeypatch.delenv("OCI_COMPARTMENT_OCID", raising=False)
    monkeypatch.delenv("OCI_COMPARTMENT_ID", raising=False)
    with pytest.raises(SystemExit, match="OCI_COMPARTMENT_OCID or OCI_COMPARTMENT_ID"):
        mod._require("OCI_COMPARTMENT_OCID", "OCI_COMPARTMENT_ID")
