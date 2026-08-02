import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError


HERE = Path(__file__).resolve().parent
REPO_SCHEMA = HERE.parent / "schemas" / "agent-extranet.schema.json"
SCHEMA_PATH = REPO_SCHEMA if REPO_SCHEMA.exists() else HERE / "agent-extranet.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
HEX = "a" * 64
DIGEST = f"sha256:{HEX}"
NOW = "2026-08-02T18:00:00Z"
LATER = "2026-08-02T19:00:00Z"
TRACE = {"traceparent": f"00-{'1' * 32}-{'2' * 16}-01"}
SIGNATURE = {"algorithm": "Ed25519", "key_id": "key:agent-a:1", "value": "base64-signature"}
STATUS = {"url": "https://identity.example/status/grant-1", "checked_at": NOW}
AUTHORITY = {
    "grant_digest": DIGEST,
    "status_ref": STATUS,
    "verifier": "keychain.example",
    "checked_at": NOW,
}


def validate(definition, instance):
    schema = {
        "$schema": SCHEMA["$schema"],
        "$id": SCHEMA["$id"],
        "$ref": f"#/$defs/{definition}",
        "$defs": SCHEMA["$defs"],
    }
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(instance)


def grant(**overrides):
    value = {
        "schema": "iac.authority-grant/0.1",
        "grant_id": "grant-1",
        "issuer": {"principal_id": "human:lehel", "kind": "human"},
        "subject_agent_id": "agent:showgo",
        "capabilities": ["repo.pull_request.comment"],
        "resources": ["github:lehelkovach/iac-bus"],
        "constraints": {"max_uses": 1, "requires_approval_for": ["repo.pull_request.comment"]},
        "issued_at": NOW,
        "expires_at": LATER,
        "nonce": "unique-nonce-1234",
        "status_ref": STATUS,
        "digest": DIGEST,
        "signature": SIGNATURE,
    }
    value.update(overrides)
    return value


def proposal(**overrides):
    value = {
        "schema": "iac.action-proposal/0.1",
        "proposal_id": "proposal-1",
        "principal_id": "human:lehel",
        "actor_agent_id": "agent:showgo",
        "capability": "repo.pull_request.comment",
        "resource": "github:lehelkovach/iac-bus#pr-9",
        "input_digest": DIGEST,
        "risk": "moderate",
        "requested_at": NOW,
        "expires_at": LATER,
        "authority": AUTHORITY,
        "semantic_refs": [{"object_id": "ksg:procedure:review-pr", "version": "1", "relation": "procedure"}],
        "trace": TRACE,
        "digest": DIGEST,
    }
    value.update(overrides)
    return value


def test_capability_grant_and_action_proposal_validate():
    validate("CapabilityGrant", grant())
    validate("ActionProposal", proposal())


def test_grant_rejects_empty_authority_scope():
    with pytest.raises(ValidationError):
        validate("CapabilityGrant", grant(capabilities=[]))
    with pytest.raises(ValidationError):
        validate("CapabilityGrant", grant(resources=[]))


def test_policy_and_digest_bound_approval_validate():
    validate(
        "PolicyDecision",
        {
            "schema": "iac.policy-decision/0.1",
            "decision_id": "decision-1",
            "proposal_digest": DIGEST,
            "policy_id": "policy:repo-comments",
            "effect": "allow",
            "requires_approval": True,
            "reasons": ["capability delegated", "human approval required"],
            "evaluated_at": NOW,
            "digest": DIGEST,
        },
    )
    validate(
        "ApprovalRecord",
        {
            "schema": "iac.approval-record/0.1",
            "approval_id": "approval-1",
            "proposal_digest": DIGEST,
            "approver": {"principal_id": "human:lehel", "kind": "human"},
            "decision": "approved",
            "decided_at": NOW,
            "expires_at": LATER,
            "digest": DIGEST,
            "signature": SIGNATURE,
        },
    )


def test_approval_rejects_unknown_decision():
    with pytest.raises(ValidationError):
        validate(
            "ApprovalRecord",
            {
                "schema": "iac.approval-record/0.1",
                "approval_id": "approval-1",
                "proposal_digest": DIGEST,
                "approver": {"principal_id": "human:lehel", "kind": "human"},
                "decision": "maybe",
                "decided_at": NOW,
                "expires_at": LATER,
                "digest": DIGEST,
                "signature": SIGNATURE,
            },
        )


def test_delivery_and_execution_receipts_are_distinct():
    validate(
        "DeliveryReceipt",
        {
            "schema": "iac.delivery-receipt/0.1",
            "receipt_id": "delivery-1",
            "message_digest": DIGEST,
            "bus_event_id": "event-1",
            "status": "delivered",
            "recipient_agent_id": "agent:worker-b",
            "recorded_at": NOW,
            "digest": DIGEST,
        },
    )
    validate(
        "ExecutionReceipt",
        {
            "schema": "iac.execution-receipt/0.1",
            "receipt_id": "execution-1",
            "proposal_digest": DIGEST,
            "actor_agent_id": "agent:worker-b",
            "capability": "repo.pull_request.comment",
            "resource": "github:lehelkovach/iac-bus#pr-9",
            "authority_digest": DIGEST,
            "policy_decision_digest": DIGEST,
            "approval_digest": DIGEST,
            "status": "succeeded",
            "result_digest": DIGEST,
            "side_effect_refs": ["github:comment:123"],
            "started_at": NOW,
            "finished_at": LATER,
            "trace": TRACE,
            "digest": DIGEST,
            "signature": SIGNATURE,
        },
    )


def test_signed_authorized_extranet_envelope_validates():
    validate(
        "ExtranetEnvelope",
        {
            "protocol": "iac-extranet/0.1",
            "message_id": "message-1",
            "message_type": "action.propose",
            "from_agent": {
                "agent_id": "agent:showgo",
                "controller_principal_id": "human:lehel",
                "key_id": "key:agent-a:1",
                "endpoint": "https://agents.example/showgo",
            },
            "to_agent_id": "agent:worker-b",
            "conversation_id": "work-42",
            "sent_at": NOW,
            "expires_at": LATER,
            "idempotency_key": "work-42:proposal-1",
            "trace": TRACE,
            "authority": AUTHORITY,
            "semantic_refs": [{"object_id": "ksg:procedure:review-pr", "relation": "procedure"}],
            "payload": proposal(),
            "payload_digest": DIGEST,
            "signature": SIGNATURE,
        },
    )


def test_envelope_requires_authority_and_rejects_unversioned_protocol():
    base = {
        "protocol": "iac-extranet/0.1",
        "message_id": "message-1",
        "message_type": "work.offer",
        "from_agent": {
            "agent_id": "agent:showgo",
            "controller_principal_id": "human:lehel",
            "key_id": "key:agent-a:1",
        },
        "to_agent_id": "agent:worker-b",
        "sent_at": NOW,
        "trace": TRACE,
        "payload": {},
        "payload_digest": DIGEST,
        "signature": SIGNATURE,
    }
    with pytest.raises(ValidationError):
        validate("ExtranetEnvelope", base)
    with pytest.raises(ValidationError):
        validate("ExtranetEnvelope", {**base, "authority": AUTHORITY, "protocol": "iac-extranet/latest"})


def test_schema_cannot_replace_runtime_authorization_checks():
    validate("CapabilityGrant", grant(expires_at="2020-01-01T00:00:00Z"))
    validate("ActionProposal", proposal(resource="github:someone-else/repo"))
    # JSON Schema verifies structure. Runtime middleware must check time,
    # revocation, target matching, attenuation, signatures, and digest binding.
