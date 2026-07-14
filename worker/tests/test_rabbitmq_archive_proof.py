from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from doc_worker.worker import ARCHIVE_VERSION, DocumentWorker

PROOF_PATH = Path(__file__).resolve().parents[1] / "scripts" / "prove_rabbitmq_archive.py"
PROOF_SPEC = importlib.util.spec_from_file_location("rabbitmq_archive_proof", PROOF_PATH)
assert PROOF_SPEC is not None and PROOF_SPEC.loader is not None
proof = importlib.util.module_from_spec(PROOF_SPEC)
PROOF_SPEC.loader.exec_module(proof)


def _isolated_configuration(**overrides):
    values = {
        "rabbitmq_url": (
            "amqp://tryscode-proof-worker:tryscode-proof-password-not-production"
            "@rabbitmq-proof:5672/%2F"
        ),
        "signing_key": "tryscode-proof-signing-key-not-production",
        "callback_token": "tryscode-proof-callback-token-not-production",
        "replay_confirmation": proof.REPLAY_CONFIRMATION,
        "expected_broker_version": "3.13.7",
    }
    values.update(overrides)
    return values


def test_proof_accepts_only_explicitly_isolated_configuration():
    proof._assert_isolated_configuration(**_isolated_configuration())


@pytest.mark.parametrize(
    "overrides",
    [
        {"rabbitmq_url": "amqp://tryscode-proof-user:tryscode-proof-pass@broker:5672/%2F"},
        {"rabbitmq_url": ("amqp://tryscode-proof-user:tryscode-proof-pass@localhost:5672/%2F")},
        {"rabbitmq_url": "amqp://guest:guest@rabbitmq-proof:5672/%2F"},
        {
            "rabbitmq_url": (
                "amqp://tryscode-proof-user:tryscode-proof-pass"
                "@rabbitmq-proof:5672/%2F?heartbeat=60"
            )
        },
        {"signing_key": "real-looking-signing-key"},
        {"callback_token": "tryscode-proof-signing-key-not-production"},
        {"replay_confirmation": "yes"},
        {"expected_broker_version": "3.13"},
        {"expected_broker_version": "4.0.1"},
    ],
)
def test_proof_rejects_non_disposable_or_ambiguous_configuration(overrides):
    with pytest.raises(RuntimeError):
        proof._assert_isolated_configuration(**_isolated_configuration(**overrides))


def test_proof_json_decoder_rejects_duplicates_and_non_objects():
    assert proof._decode_json_object(b'{"safe":true}') == {"safe": True}

    with pytest.raises(ValueError):
        proof._decode_json_object(b'{"safe":true,"safe":false}')
    with pytest.raises(ValueError):
        proof._decode_json_object(b"[]")
    with pytest.raises(ValueError):
        proof._decode_json_object("é".encode())


def test_archive_validator_requires_the_exact_bounded_contract():
    source = b'{"private":"full pedagogical source"}'
    envelope = {
        "archive_version": ARCHIVE_VERSION,
        "attempts": 3,
        "failure_code": "processing-failed",
        "message_digest": DocumentWorker._message_fingerprint(source),
        "message_size": len(source),
    }
    headers = {
        "x-tryscode-document-archive-version": ARCHIVE_VERSION,
        "x-tryscode-document-archive-reason": "processing-failed",
    }

    assert (
        proof._validate_archive(
            archive_body=json.dumps(envelope).encode("ascii"),
            archive_headers=headers,
            source_body=source,
            max_retries=2,
        )
        == envelope
    )

    with pytest.raises(AssertionError):
        proof._validate_archive(
            archive_body=json.dumps({**envelope, "source": source.decode()}).encode("ascii"),
            archive_headers=headers,
            source_body=source,
            max_retries=2,
        )
    with pytest.raises(AssertionError):
        proof._validate_archive(
            archive_body=json.dumps(envelope).encode("ascii"),
            archive_headers={**headers, "x-incoming-secret": "forbidden"},
            source_body=source,
            max_retries=2,
        )


def test_failure_sequence_requires_real_retry_deaths_and_confirm_before_ack():
    events = [
        {"sequence": 1, "phase": "failure", "event": "delivery_received", "attempt": 0},
        {
            "sequence": 2,
            "phase": "failure",
            "event": "publish_confirmed",
            "queue": "document_tasks.retry",
        },
        {"sequence": 3, "phase": "failure", "event": "source_ack_succeeded"},
        {
            "sequence": 4,
            "phase": "failure",
            "event": "delivery_received",
            "attempt": 1,
            "retry_death": {
                "queue": "document_tasks.retry",
                "reason": "expired",
                "count": 1,
            },
        },
        {
            "sequence": 5,
            "phase": "failure",
            "event": "publish_confirmed",
            "queue": "document_tasks.retry",
        },
        {"sequence": 6, "phase": "failure", "event": "source_ack_succeeded"},
        {
            "sequence": 7,
            "phase": "failure",
            "event": "delivery_received",
            "attempt": 2,
            "retry_death": {
                "queue": "document_tasks.retry",
                "reason": "expired",
                "count": 1,
            },
        },
        {
            "sequence": 8,
            "phase": "failure",
            "event": "publish_confirmed",
            "queue": "document_tasks.archive",
        },
        {
            "sequence": 9,
            "phase": "failure",
            "event": "callback_received",
            "status": "failed",
        },
        {"sequence": 10, "phase": "failure", "event": "source_ack_succeeded"},
    ]

    proof._validate_failure_sequence(events, max_retries=2)

    invalid = [dict(event) for event in events]
    invalid[4]["sequence"] = 6
    invalid[5]["sequence"] = 5
    with pytest.raises(AssertionError):
        proof._validate_failure_sequence(invalid, max_retries=2)
