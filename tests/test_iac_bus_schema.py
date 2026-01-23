import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, RefResolver, ValidationError


ROOT = Path(__file__).resolve().parents[1]


def _load_schema(name: str):
    return json.loads((ROOT / "schemas" / name).read_text(encoding="utf-8"))


def _message_validator():
    iac_schema = _load_schema("iac-bus.schema.json")
    orchestration_schema = _load_schema("orchestration.schema.json")
    schema = {
        "$schema": iac_schema["$schema"],
        "$id": iac_schema["$id"],
        "$ref": "#/$defs/MessageCreate",
        "$defs": iac_schema["$defs"],
    }
    resolver = RefResolver.from_schema(
        schema,
        store={
            orchestration_schema["$id"]: orchestration_schema,
        },
    )
    return Draft202012Validator(schema, resolver=resolver)


def test_orchestration_job_message_valid():
    validator = _message_validator()
    payload = {
        "type": "orchestration.job",
        "message": {
            "job": {
                "job_id": "job-1",
                "name": "build-service",
                "steps": [
                    {"id": "design"},
                    {"id": "impl", "depends_on": [{"step_id": "design"}]},
                ],
            }
        },
    }
    validator.validate(payload)


def test_orchestration_step_assign_valid():
    validator = _message_validator()
    payload = {
        "type": "orchestration.step.assign",
        "message": {
            "job_id": "job-1",
            "step": {
                "id": "impl",
                "depends_on": [{"step_id": "design"}],
            },
        },
    }
    validator.validate(payload)


def test_orchestration_step_status_valid():
    validator = _message_validator()
    payload = {
        "type": "orchestration.step.status",
        "message": {
            "job_id": "job-1",
            "step_id": "impl",
            "status": "completed",
            "note": "done",
        },
    }
    validator.validate(payload)


def test_orchestration_job_message_invalid():
    validator = _message_validator()
    payload = {
        "type": "orchestration.job",
        "message": {
            "job": {
                "name": "missing-job-id",
                "steps": [{"id": "one"}],
            }
        },
    }
    with pytest.raises(ValidationError):
        validator.validate(payload)
