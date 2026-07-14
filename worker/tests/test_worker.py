from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pika
import pytest
from pydantic import ValidationError

from doc_worker.config import Settings, validate_settings
from doc_worker.renderer import DocumentValidationError
from doc_worker.worker import (
    ARCHIVE_VERSION,
    ATTEMPT_HEADER,
    CALLBACK_PENDING_HEADER,
    MAX_MESSAGE_BYTES,
    RETRY_SIGNATURE_HEADER,
    RETRY_STATE_VERSION,
    RETRY_STATE_VERSION_HEADER,
    DocumentWorker,
    PermanentDocumentError,
    WorkerReconnectRequired,
)

RETRY_SIGNING_KEY = "test-only-document-retry-key-001"


def _settings(**overrides):
    values = {
        "document_queue": "document_tasks",
        "document_retry_queue": "document_tasks.retry",
        "document_archive_queue": "document_tasks.archive",
        "rabbitmq_url": "amqp://guest:guest@rabbitmq:5672/%2F",
        "rabbitmq_socket_timeout_seconds": 3.0,
        "rabbitmq_stack_timeout_seconds": 5.0,
        "rabbitmq_blocked_timeout_seconds": 3.0,
        "rabbitmq_heartbeat_seconds": 30,
        "document_max_retries": 2,
        "document_retry_delay_seconds": 1,
        "document_retry_signing_key": RETRY_SIGNING_KEY,
        "document_retry_max_messages": 20,
        "document_retry_max_bytes": 10 * 1024 * 1024,
        "document_archive_ttl_ms": 60_000,
        "document_archive_max_messages": 10,
        "document_output_dir": "/tmp/document-worker-tests",
        "document_storage_mode": "local",
        "document_storage_prefix": "documents",
        "harmony_callback_url": "",
        "harmony_service_token": "",
        "document_callback_timeout_seconds": 1,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class RecordingChannel:
    def __init__(
        self,
        *,
        publish_error: Exception | None = None,
        ack_error: Exception | None = None,
    ):
        self.events = []
        self.publish_error = publish_error
        self.ack_error = ack_error

    def queue_declare(self, **kwargs):
        self.events.append(("declare", kwargs))

    def confirm_delivery(self):
        self.events.append(("confirm",))

    def basic_publish(self, **kwargs):
        self.events.append(("publish", kwargs))
        if self.publish_error is not None:
            raise self.publish_error

    def basic_ack(self, delivery_tag):
        self.events.append(("ack", delivery_tag))
        if self.ack_error is not None:
            raise self.ack_error

    def basic_nack(self, delivery_tag, *, requeue):
        self.events.append(("nack", delivery_tag, requeue))

    def stop_consuming(self):
        self.events.append(("stop",))


def _method(tag=41):
    return SimpleNamespace(delivery_tag=tag)


def _properties(headers=None):
    return SimpleNamespace(headers=headers or {})


def _published(channel: RecordingChannel):
    return next(event[1] for event in channel.events if event[0] == "publish")


def _retry_headers(
    worker: DocumentWorker,
    body: bytes,
    attempt: int,
    *,
    mode: str | None = None,
    **overrides,
):
    headers = {
        "x-death": [
            {
                "count": 1,
                "queue": "document_tasks.retry",
                "reason": "expired",
            }
        ],
        **worker._retry_headers(body, attempt=attempt, mode=mode),
    }
    headers.update(overrides)
    return headers


def test_topology_enables_confirms_after_bounded_archive_declaration():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel()

    worker._declare_topology(channel)

    assert channel.events == [
        ("declare", {"queue": "document_tasks", "durable": True}),
        (
            "declare",
            {
                "queue": "document_tasks.retry",
                "durable": True,
                "arguments": {
                    "x-queue-type": "quorum",
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": "document_tasks",
                    "x-dead-letter-strategy": "at-least-once",
                    "x-overflow": "reject-publish",
                    "x-max-length": 20,
                    "x-max-length-bytes": 10 * 1024 * 1024,
                },
            },
        ),
        (
            "declare",
            {
                "queue": "document_tasks.archive",
                "durable": True,
                "arguments": {
                    "x-message-ttl": 60_000,
                    "x-max-length": 10,
                    "x-overflow": "drop-head",
                },
            },
        ),
        ("confirm",),
    ]


def test_confirmed_publish_is_persistent_mandatory_and_accepts_none_return():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel()

    worker._publish_confirmed(
        channel,
        "document_tasks.retry",
        b"payload",
        {"x-tryscode-document-attempt": 1},
        delay_ms=1_000,
    )

    published = _published(channel)
    assert published["exchange"] == ""
    assert published["routing_key"] == "document_tasks.retry"
    assert published["body"] == b"payload"
    assert published["mandatory"] is True
    assert published["properties"].content_type == "application/json"
    assert published["properties"].delivery_mode == 2
    assert published["properties"].expiration == "1000"
    assert published["properties"].headers == {"x-tryscode-document-attempt": 1}


def test_transient_failure_confirms_identical_retry_before_ack():
    worker = DocumentWorker(_settings())
    worker._handle = Mock(side_effect=RuntimeError("temporary"))
    channel = RecordingChannel()
    body = b'{"feedback":"private pedagogical text"}'

    worker._consume(
        channel,
        _method(),
        _properties({"incoming-secret": "must-not-be-copied"}),
        body,
    )

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    published = _published(channel)
    assert published["routing_key"] == "document_tasks.retry"
    assert published["body"] == body
    headers = published["properties"].headers
    assert headers[ATTEMPT_HEADER] == 1
    assert headers[RETRY_STATE_VERSION_HEADER] == RETRY_STATE_VERSION
    assert len(headers[RETRY_SIGNATURE_HEADER]) == 64
    assert "incoming-secret" not in headers


def test_storage_configuration_failure_is_retried_not_archived():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel()
    document = SimpleNamespace(job_id="job-2026-01")
    body = b'{"action":"render_subject_pdf"}'

    with (
        patch("doc_worker.worker.parse_document", return_value=document),
        patch("doc_worker.worker.render_document", return_value="/tmp/document.pdf"),
        patch(
            "doc_worker.worker.publish_document",
            side_effect=RuntimeError("invalid document storage prefix"),
        ),
    ):
        worker._consume(channel, _method(), _properties(), body)

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    assert _published(channel)["routing_key"] == "document_tasks.retry"


def test_rendering_failure_is_retried_even_if_renderer_uses_validation_error():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel()
    document = SimpleNamespace(job_id="job-2026-01")
    body = b'{"action":"render_subject_pdf"}'

    with (
        patch("doc_worker.worker.parse_document", return_value=document),
        patch(
            "doc_worker.worker.render_document",
            side_effect=DocumentValidationError("unsafe output path"),
        ),
    ):
        worker._consume(channel, _method(), _properties(), body)

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    assert _published(channel)["routing_key"] == "document_tasks.retry"


def test_retry_publish_failure_requeues_and_stops_without_ack():
    worker = DocumentWorker(_settings())
    worker._handle = Mock(side_effect=RuntimeError("temporary"))
    channel = RecordingChannel(publish_error=RuntimeError("publish failed"))

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"payload")

    assert [event[0] for event in channel.events] == [
        "publish",
        "nack",
        "stop",
    ]
    assert ("ack", 41) not in channel.events
    assert ("nack", 41, True) in channel.events


def test_invalid_document_archives_only_a_bounded_minimal_envelope():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel()
    body = json.dumps(
        {
            "action": "unknown",
            "job_id": "learner-name-in-id",
            "participant_names": ["Private Learner"],
            "feedback": "Secret feedback",
            "evidence_url": "https://private.example/evidence",
        }
    ).encode("utf-8")

    worker._consume(
        channel,
        _method(),
        _properties({"authorization": "secret-header"}),
        body,
    )

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    published = _published(channel)
    assert published["routing_key"] == "document_tasks.archive"
    archive = json.loads(published["body"])
    assert archive == {
        "archive_version": ARCHIVE_VERSION,
        "attempts": 1,
        "failure_code": "invalid-document",
        "message_digest": worker._message_fingerprint(body),
        "message_size": len(body),
    }
    serialized = published["body"].decode("ascii")
    for forbidden in (
        "Private Learner",
        "Secret feedback",
        "private.example",
        "secret-header",
        "learner-name-in-id",
    ):
        assert forbidden not in serialized
    assert published["properties"].headers == {
        "x-tryscode-document-archive-version": ARCHIVE_VERSION,
        "x-tryscode-document-archive-reason": "invalid-document",
    }


def test_archive_publish_failure_requeues_source_without_ack():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel(publish_error=RuntimeError("publish failed"))

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"not-json")

    assert [event[0] for event in channel.events] == [
        "publish",
        "nack",
        "stop",
    ]
    assert not any(event[0] == "ack" for event in channel.events)


def test_exhausted_retry_archives_minimal_processing_failure_then_acks():
    worker = DocumentWorker(_settings(document_max_retries=2))
    worker._handle = Mock(side_effect=RuntimeError("callback unavailable"))
    channel = RecordingChannel()
    body = b'{"feedback":"never archive me"}'

    worker._consume(
        channel,
        _method(),
        _properties({**_retry_headers(worker, body, 2), "secret": "header"}),
        body,
    )

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    archive = json.loads(_published(channel)["body"])
    assert archive["attempts"] == 3
    assert archive["failure_code"] == "processing-failed"
    assert "never archive me" not in json.dumps(archive)
    assert "header" not in json.dumps(archive)


def test_exhausted_valid_job_notifies_harmony_failure_before_ack():
    worker = DocumentWorker(_settings(document_max_retries=2))
    worker._handle = Mock(side_effect=RuntimeError("callback unavailable"))
    channel = RecordingChannel()
    body = b'{"action":"render_subject_pdf","job_id":"job-2026-01"}'
    trusted = SimpleNamespace(job_id="job-2026-01")

    with (
        patch("doc_worker.worker.parse_document", return_value=trusted),
        patch("doc_worker.worker.notify_failure") as failure,
    ):
        worker._consume(
            channel,
            _method(),
            _properties(_retry_headers(worker, body, 2)),
            body,
        )

    failure.assert_called_once_with(
        worker.settings,
        job_id="job-2026-01",
        failure_code="document-processing-failed",
    )
    assert [event[0] for event in channel.events] == ["publish", "ack"]


def test_failure_callback_error_becomes_a_compact_delayed_continuation():
    worker = DocumentWorker(_settings(document_max_retries=2))
    worker._handle = Mock(side_effect=RuntimeError("processing failed"))
    channel = RecordingChannel()
    body = b'{"action":"render_subject_pdf","job_id":"job-2026-01"}'

    with (
        patch(
            "doc_worker.worker.parse_document",
            return_value=SimpleNamespace(job_id="job-2026-01"),
        ),
        patch(
            "doc_worker.worker.notify_failure",
            side_effect=RuntimeError("callback unavailable"),
        ),
    ):
        worker._consume(
            channel,
            _method(),
            _properties(_retry_headers(worker, body, 2)),
            body,
        )

    assert [event[0] for event in channel.events] == [
        "publish",
        "publish",
        "ack",
    ]
    callback_publish = channel.events[1][1]
    assert callback_publish["routing_key"] == "document_tasks.retry"
    assert json.loads(callback_publish["body"])["mode"] == "failure"
    assert b"render_subject_pdf" not in callback_publish["body"]


def test_s3_success_removes_only_the_local_staging_pdf(tmp_path):
    worker = DocumentWorker(_settings(document_storage_mode="s3"))
    staged = tmp_path / "document.pdf"
    staged.write_bytes(b"pdf")
    document = SimpleNamespace(job_id="job-2026-01")
    published = SimpleNamespace(
        storage_key="documents/document.pdf",
        size_bytes=5,
        sha256="a" * 64,
        storage_version="version-one",
    )
    body = b'{"action":"render_subject_pdf","job_id":"job-2026-01"}'

    with (
        patch("doc_worker.worker.parse_document", return_value=document),
        patch("doc_worker.worker.render_document", return_value=staged),
        patch("doc_worker.worker.publish_document", return_value=published),
        patch("doc_worker.worker.notify_success"),
    ):
        worker._handle(body)

    assert not staged.exists()


def test_success_callback_retry_uses_compact_state_and_never_rerenders(tmp_path):
    worker = DocumentWorker(_settings())
    staged = tmp_path / "document.pdf"
    staged.write_bytes(b"pdf")
    document = SimpleNamespace(job_id="job-2026-01")
    published_artifact = SimpleNamespace(
        storage_key="documents/document.pdf",
        size_bytes=5,
        sha256="a" * 64,
        storage_version="version-one",
    )
    source_body = json.dumps(
        {
            "action": "render_certificate_pdf",
            "job_id": "job-2026-01",
            "learner_name": "Private Learner",
        }
    ).encode()
    first_channel = RecordingChannel()

    with (
        patch("doc_worker.worker.parse_document", return_value=document),
        patch("doc_worker.worker.render_document", return_value=staged),
        patch("doc_worker.worker.publish_document", return_value=published_artifact),
        patch(
            "doc_worker.worker.notify_success",
            side_effect=RuntimeError("callback unavailable"),
        ),
    ):
        worker._consume(first_channel, _method(), _properties(), source_body)

    assert [event[0] for event in first_channel.events] == ["publish", "ack"]
    continuation = _published(first_channel)
    assert continuation["routing_key"] == "document_tasks.retry"
    assert json.loads(continuation["body"])["mode"] == "success"
    assert b"Private Learner" not in continuation["body"]
    assert continuation["properties"].headers[CALLBACK_PENDING_HEADER] == "success"

    replay_headers = {
        **continuation["properties"].headers,
        "x-death": [
            {
                "count": 1,
                "queue": "document_tasks.retry",
                "reason": "expired",
            }
        ],
    }
    second_channel = RecordingChannel()
    worker._handle = Mock(side_effect=AssertionError("must not rerender"))
    with patch("doc_worker.worker.notify_success") as callback:
        worker._consume(
            second_channel,
            _method(42),
            _properties(replay_headers),
            continuation["body"],
        )

    worker._handle.assert_not_called()
    callback.assert_called_once()
    assert second_channel.events == [("ack", 42)]


@pytest.mark.parametrize(
    "forged_headers",
    [
        {ATTEMPT_HEADER: 1, CALLBACK_PENDING_HEADER: "failure"},
        {ATTEMPT_HEADER: 1, CALLBACK_PENDING_HEADER: []},
        {ATTEMPT_HEADER: 1, CALLBACK_PENDING_HEADER: {}},
        {
            ATTEMPT_HEADER: 1,
            RETRY_STATE_VERSION_HEADER: RETRY_STATE_VERSION,
            RETRY_SIGNATURE_HEADER: "é",
        },
    ],
)
def test_forged_internal_retry_headers_are_archived_without_callback_or_render(
    forged_headers,
):
    worker = DocumentWorker(_settings())
    body = b'{"action":"render_subject_pdf","job_id":"job-forged"}'
    worker._handle = Mock(side_effect=AssertionError("must not render"))
    channel = RecordingChannel()

    worker._consume(
        channel,
        _method(),
        _properties(
            {
                **forged_headers,
                "x-death": [
                    {
                        "count": 1,
                        "queue": "document_tasks.retry",
                        "reason": "expired",
                    }
                ],
            }
        ),
        body,
    )

    worker._handle.assert_not_called()
    assert [event[0] for event in channel.events] == ["publish", "ack"]
    assert json.loads(_published(channel)["body"])["failure_code"] == ("invalid-retry-state")


def test_oversized_internal_state_is_rejected_before_hmac():
    worker = DocumentWorker(_settings())
    worker._retry_signature = Mock(
        side_effect=AssertionError("oversized state must be rejected before HMAC")
    )
    body = b"x" * (MAX_MESSAGE_BYTES + 1)
    channel = RecordingChannel()

    worker._consume(
        channel,
        _method(),
        _properties({ATTEMPT_HEADER: 1}),
        body,
    )

    worker._retry_signature.assert_not_called()
    assert [event[0] for event in channel.events] == ["publish", "ack"]
    assert json.loads(_published(channel)["body"])["failure_code"] == ("invalid-retry-state")


def test_callback_delivery_is_archived_after_its_bounded_retry_budget():
    worker = DocumentWorker(_settings(document_max_retries=2))
    body = worker._callback_body(
        mode="failure",
        job_id="job-2026-01",
        failure_code="document-processing-failed",
    )
    properties = _properties(_retry_headers(worker, body, 2, mode="failure"))
    channel = RecordingChannel()

    with patch(
        "doc_worker.worker.notify_failure",
        side_effect=RuntimeError("callback unavailable"),
    ):
        worker._consume(channel, _method(), properties, body)

    assert [event[0] for event in channel.events] == ["publish", "ack"]
    assert json.loads(_published(channel)["body"])["failure_code"] == ("callback-failed")


def test_successful_handle_ack_error_never_publishes_a_retry():
    worker = DocumentWorker(_settings())
    worker._handle = Mock(return_value="done")
    channel = RecordingChannel(ack_error=RuntimeError("connection lost"))

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"payload")

    assert [event[0] for event in channel.events] == ["ack", "stop"]


def test_retry_publish_ack_error_never_publishes_a_second_copy():
    worker = DocumentWorker(_settings())
    worker._handle = Mock(side_effect=RuntimeError("temporary"))
    channel = RecordingChannel(ack_error=RuntimeError("connection lost"))

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"payload")

    assert [event[0] for event in channel.events] == ["publish", "ack", "stop"]


def test_archive_publish_ack_error_never_publishes_a_second_archive():
    worker = DocumentWorker(_settings())
    channel = RecordingChannel(ack_error=RuntimeError("connection lost"))

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"not-json")

    assert [event[0] for event in channel.events] == ["publish", "ack", "stop"]


def test_oversized_message_is_rejected_before_json_decode():
    worker = DocumentWorker(_settings())
    body = b"x" * (MAX_MESSAGE_BYTES + 1)

    with patch("doc_worker.worker.json.loads") as loads:
        with pytest.raises(PermanentDocumentError) as raised:
            worker._handle(body)

    assert raised.value.failure_code == "message-too-large"
    loads.assert_not_called()


@pytest.mark.parametrize("body", [b"\xff", b"{", b""])
def test_malformed_json_is_a_permanent_bounded_failure(body):
    worker = DocumentWorker(_settings())

    with pytest.raises(PermanentDocumentError) as raised:
        worker._handle(body)

    assert raised.value.failure_code == "invalid-json"


def test_attempt_requires_a_bounded_integer_and_retry_dead_letter_proof():
    worker = DocumentWorker(_settings(document_max_retries=3))
    body = b'{"action":"render_subject_pdf"}'

    assert worker._attempt(_properties(_retry_headers(worker, body, 2)), body) == 2
    broker_headers = _retry_headers(worker, body, 2)
    broker_headers["x-death"][0]["count"] = pika.compat.long(1)
    assert worker._attempt(_properties(broker_headers), body) == 2
    boolean_death = _retry_headers(worker, body, 2)
    boolean_death["x-death"][0]["count"] = True
    assert worker._attempt(_properties(boolean_death), body) == 0
    assert (
        worker._attempt(
            _properties(_retry_headers(worker, body, 2)),
            body + b"tampered",
        )
        == 0
    )
    for invalid_value in (True, 1.0, "1", -1, 0, 4, 999):
        assert worker._attempt(_properties(_retry_headers(worker, body, invalid_value)), body) == 0
    assert worker._attempt(_properties({ATTEMPT_HEADER: 2}), body) == 0
    assert (
        worker._attempt(
            _properties(
                _retry_headers(
                    worker,
                    body,
                    2,
                    **{
                        "x-death": [
                            {
                                "count": 1,
                                "queue": "another.retry",
                                "reason": "expired",
                            }
                        ]
                    },
                )
            ),
            body,
        )
        == 0
    )
    late_valid_death = [
        {"count": 1, "queue": "unrelated", "reason": "expired"} for _ in range(16)
    ] + _retry_headers(worker, body, 2)["x-death"]
    assert (
        worker._attempt(
            _properties(
                {
                    **worker._retry_headers(body, attempt=2),
                    "x-death": late_valid_death,
                }
            ),
            body,
        )
        == 0
    )


@pytest.mark.parametrize(
    "publish_error",
    [
        pika.exceptions.UnroutableError([]),
        pika.exceptions.NackError([]),
    ],
)
def test_pika_confirm_failures_requeue_source_and_force_reconnect(publish_error):
    worker = DocumentWorker(_settings())
    worker._handle = Mock(side_effect=RuntimeError("temporary"))
    channel = RecordingChannel(publish_error=publish_error)

    with pytest.raises(WorkerReconnectRequired):
        worker._consume(channel, _method(), _properties(), b"payload")

    assert [event[0] for event in channel.events] == ["publish", "nack", "stop"]


def test_start_applies_bounded_connection_parameters_and_backoff():
    worker = DocumentWorker(_settings())
    channel = Mock()
    channel.start_consuming.side_effect = WorkerReconnectRequired("reconnect")
    connection = Mock()
    connection.channel.return_value = channel
    connection.is_open = True

    with (
        patch(
            "doc_worker.worker.pika.BlockingConnection",
            side_effect=[connection, KeyboardInterrupt()],
        ) as connect,
        patch.object(worker, "_declare_topology"),
        patch("doc_worker.worker.time.sleep") as sleep,
    ):
        worker.start()

    params = connect.call_args_list[0].args[0]
    assert params.socket_timeout == 3.0
    assert params.stack_timeout == 5.0
    assert params.blocked_connection_timeout == 3.0
    assert params.heartbeat == 30
    assert params.connection_attempts == 1
    assert params.retry_delay == 0
    sleep.assert_called_once_with(3)
    connection.close.assert_called_once_with()


def test_settings_reject_invalid_archive_bounds_and_ambiguous_queues():
    with pytest.raises(ValidationError):
        Settings(rabbitmq_url="amqp://rabbit", document_archive_ttl_ms=59_999)
    with pytest.raises(ValidationError):
        Settings(rabbitmq_url="amqp://rabbit", document_archive_max_messages=0)

    settings = Settings(
        rabbitmq_url="amqp://rabbit",
        document_retry_queue="document_tasks",
    )
    with pytest.raises(RuntimeError, match="distinctes"):
        validate_settings(settings)

    with pytest.raises(RuntimeError, match="STORAGE_PREFIX"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                document_storage_prefix="../documents",
            )
        )
    with pytest.raises(RuntimeError, match="STACK_TIMEOUT"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                rabbitmq_socket_timeout_seconds=5,
                rabbitmq_stack_timeout_seconds=5,
            )
        )
    with pytest.raises(RuntimeError, match="callback Harmony"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                document_retry_signing_key=RETRY_SIGNING_KEY,
                harmony_callback_url="https://user:secret@harmony/callback",
                harmony_service_token="valid-token",
            )
        )
    with pytest.raises(RuntimeError, match="callback Harmony"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                document_retry_signing_key=RETRY_SIGNING_KEY,
                harmony_callback_url="https://harmony/api/v1/jobs/callback",
                harmony_service_token="token with whitespace",
            )
        )
    with pytest.raises(RuntimeError, match="requis"):
        validate_settings(Settings(rabbitmq_url="amqp://rabbit"))

    with pytest.raises(RuntimeError, match="RETRY_SIGNING_KEY"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                harmony_callback_url="https://harmony/api/v1/jobs/callback",
                harmony_service_token="valid-token",
            )
        )

    with pytest.raises(RuntimeError, match="distinct"):
        validate_settings(
            Settings(
                rabbitmq_url="amqp://rabbit",
                document_retry_signing_key=RETRY_SIGNING_KEY,
                harmony_callback_url="https://harmony/api/v1/jobs/callback",
                harmony_service_token=RETRY_SIGNING_KEY,
            )
        )
