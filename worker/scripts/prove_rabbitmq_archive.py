#!/usr/bin/env python3
"""Exercise the document retry/archive lifecycle against a real RabbitMQ.

This proof is intentionally self-contained and destructive only to the three
document queues in a disposable broker selected by the companion shell script.
It imports the production worker, uses real Pika publisher confirms and broker
dead-lettering, and records only bounded technical events.  It never serializes
the source document, retry HMAC, callback credential, or AMQP password.

The proof closes the worker/RabbitMQ part of requirement 173.  It cannot prove
the Sauron UI without running Sauron and Harmony, so that criterion remains
explicitly ``not_run`` in the emitted result.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pika

from doc_worker.config import Settings, validate_settings
from doc_worker.worker import (
    ARCHIVE_VERSION,
    ATTEMPT_HEADER,
    CALLBACK_PENDING_HEADER,
    RETRY_STATE_VERSION_HEADER,
    DocumentWorker,
)

PROOF_VERSION = "tryscode.document-archive-proof.v1"
REPLAY_CONFIRMATION = "REPLAY_DISPOSABLE_DOCUMENT_ARCHIVE_PROOF"
EXPECTED_CALLBACK_PATH = "/api/v1/jobs/callback"
MAX_CALLBACK_BYTES = 32 * 1024
PROOF_JOB_ID = "proof-document-archive-job"
REPLAY_JOB_ID = "proof-document-archive-replay"
PEDAGOGICAL_SENTINEL = "FULL_PEDAGOGICAL_DOCUMENT_MUST_NOT_BE_ARCHIVED"
FAKE_SECRET_SENTINEL = "PROOF_FAKE_SECRET_MUST_NOT_BE_ARCHIVED"
HEADER_SENTINEL = "PROOF_INCOMING_HEADER_MUST_NOT_BE_ARCHIVED"
SAFE_FAILURE_CODE = "document-processing-failed"


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _assert_isolated_configuration(
    *,
    rabbitmq_url: str,
    signing_key: str,
    callback_token: str,
    replay_confirmation: str,
    expected_broker_version: str,
) -> None:
    """Refuse any broker or credential not reserved for this disposable proof."""

    try:
        parsed = urlsplit(rabbitmq_url)
        port = parsed.port
    except ValueError:
        raise RuntimeError("proof RabbitMQ URL is invalid") from None
    vhost = unquote(parsed.path.lstrip("/"))
    if (
        parsed.scheme != "amqp"
        or parsed.hostname != "rabbitmq-proof"
        or port not in {None, 5672}
        or not isinstance(parsed.username, str)
        or not parsed.username.startswith("tryscode-proof-")
        or not isinstance(parsed.password, str)
        or not parsed.password.startswith("tryscode-proof-")
        or vhost not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("proof refuses a non-disposable RabbitMQ configuration")
    if (
        not signing_key.startswith("tryscode-proof-")
        or not callback_token.startswith("tryscode-proof-")
        or signing_key == callback_token
    ):
        raise RuntimeError("proof credentials are not isolated or are ambiguous")
    if replay_confirmation != REPLAY_CONFIRMATION:
        raise RuntimeError("controlled replay confirmation is missing")
    if not re.fullmatch(r"3\.13\.\d+", expected_broker_version):
        raise RuntimeError("proof requires an explicit RabbitMQ 3.13 patch version")


def _decode_json_object(body: bytes) -> dict[str, object]:
    def reject_duplicates(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    try:
        value = json.loads(
            body.decode("ascii", errors="strict"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise ValueError("invalid proof JSON object") from None
    if not isinstance(value, dict):
        raise ValueError("invalid proof JSON object")
    return value


def _normal_text(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            return value.decode("ascii", errors="strict")
        except UnicodeError:
            return None
    return value if isinstance(value, str) else None


class _EventRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_ns = time.monotonic_ns()
        self._events: list[dict[str, object]] = []

    def add(self, event: str, *, phase: str, **fields: object) -> None:
        with self._lock:
            self._events.append(
                {
                    "sequence": len(self._events) + 1,
                    "elapsed_ms": (time.monotonic_ns() - self._started_ns) // 1_000_000,
                    "phase": phase,
                    "event": event,
                    **fields,
                }
            )

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(event) for event in self._events]


def _safe_death_proof(headers: object, retry_queue: str) -> dict[str, object] | None:
    if not isinstance(headers, dict):
        return None
    deaths = headers.get("x-death")
    if not isinstance(deaths, list):
        return None
    for death in deaths[:16]:
        if not isinstance(death, dict):
            continue
        queue = _normal_text(death.get("queue"))
        reason = _normal_text(death.get("reason"))
        count = death.get("count")
        if (
            queue == retry_queue
            and reason == "expired"
            and isinstance(count, int)
            and not isinstance(count, bool)
        ):
            return {"queue": queue, "reason": reason, "count": int(count)}
    return None


class _InstrumentedChannel:
    """Record safe ordering around real synchronous Pika operations."""

    def __init__(
        self,
        channel,
        *,
        recorder: _EventRecorder,
        phase: str,
        stop_after_archive_ack: bool = False,
        stop_after_next_ack: bool = False,
    ) -> None:
        self._channel = channel
        self._recorder = recorder
        self._phase = phase
        self._stop_after_archive_ack = stop_after_archive_ack
        self._stop_after_next_ack = stop_after_next_ack
        self._archive_was_confirmed = False

    def queue_declare(self, **kwargs):
        arguments = kwargs.get("arguments")
        safe_arguments = dict(arguments) if isinstance(arguments, dict) else None
        self._recorder.add(
            "queue_declare_begin",
            phase=self._phase,
            queue=kwargs.get("queue"),
            durable=kwargs.get("durable"),
            arguments=safe_arguments,
        )
        result = self._channel.queue_declare(**kwargs)
        self._recorder.add(
            "queue_declared",
            phase=self._phase,
            queue=kwargs.get("queue"),
        )
        return result

    def confirm_delivery(self):
        self._channel.confirm_delivery()
        self._recorder.add("publisher_confirm_mode_enabled", phase=self._phase)

    def basic_publish(self, **kwargs):
        queue = kwargs.get("routing_key")
        properties = kwargs.get("properties")
        headers = getattr(properties, "headers", None)
        safe_headers: dict[str, object] = {}
        if isinstance(headers, dict):
            for name in (
                ATTEMPT_HEADER,
                CALLBACK_PENDING_HEADER,
                RETRY_STATE_VERSION_HEADER,
                "x-tryscode-document-archive-version",
                "x-tryscode-document-archive-reason",
            ):
                if name in headers:
                    safe_headers[name] = headers[name]
        self._recorder.add(
            "publish_begin",
            phase=self._phase,
            queue=queue,
            mandatory=kwargs.get("mandatory"),
            delivery_mode=getattr(properties, "delivery_mode", None),
            headers=safe_headers,
        )
        result = self._channel.basic_publish(**kwargs)
        if result is False:
            raise RuntimeError("RabbitMQ negatively acknowledged a proof publication")
        self._recorder.add(
            "publish_confirmed",
            phase=self._phase,
            queue=queue,
        )
        if queue == "document_tasks.archive":
            self._archive_was_confirmed = True
        return result

    def basic_ack(self, delivery_tag):
        self._recorder.add(
            "source_ack_begin",
            phase=self._phase,
            delivery_tag=int(delivery_tag),
        )
        result = self._channel.basic_ack(delivery_tag)
        self._recorder.add(
            "source_ack_succeeded",
            phase=self._phase,
            delivery_tag=int(delivery_tag),
        )
        if self._stop_after_next_ack or (
            self._stop_after_archive_ack and self._archive_was_confirmed
        ):
            self._channel.stop_consuming()
        return result

    def basic_nack(self, delivery_tag, *, requeue):
        self._recorder.add(
            "source_nack",
            phase=self._phase,
            delivery_tag=int(delivery_tag),
            requeue=bool(requeue),
        )
        return self._channel.basic_nack(delivery_tag, requeue=requeue)

    def stop_consuming(self):
        self._recorder.add("consumer_stop_requested", phase=self._phase)
        return self._channel.stop_consuming()

    def __getattr__(self, name):
        return getattr(self._channel, name)


def _connection_parameters(settings: Settings) -> pika.URLParameters:
    parameters = pika.URLParameters(settings.rabbitmq_url)
    parameters.socket_timeout = settings.rabbitmq_socket_timeout_seconds
    parameters.stack_timeout = settings.rabbitmq_stack_timeout_seconds
    parameters.blocked_connection_timeout = settings.rabbitmq_blocked_timeout_seconds
    parameters.heartbeat = settings.rabbitmq_heartbeat_seconds
    parameters.connection_attempts = 1
    parameters.retry_delay = 0
    return parameters


def _server_version(connection) -> str:
    implementation = getattr(connection, "_impl", None)
    properties = getattr(implementation, "server_properties", None)
    if not isinstance(properties, dict):
        raise RuntimeError("RabbitMQ did not expose server properties")
    value = properties.get("version")
    if value is None:
        value = properties.get(b"version")
    version = _normal_text(value)
    if version is None:
        raise RuntimeError("RabbitMQ did not expose a valid server version")
    return version


class _WorkerPhase:
    def __init__(
        self,
        worker: DocumentWorker,
        *,
        recorder: _EventRecorder,
        phase: str,
        expected_broker_version: str,
        stop_after_archive_ack: bool = False,
        stop_after_next_ack: bool = False,
    ) -> None:
        self.worker = worker
        self.recorder = recorder
        self.phase = phase
        self.expected_broker_version = expected_broker_version
        self.stop_after_archive_ack = stop_after_archive_ack
        self.stop_after_next_ack = stop_after_next_ack
        self.ready = threading.Event()
        self._connection = None
        self._channel = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"document-archive-proof-{phase}",
            daemon=True,
        )

    def _run(self) -> None:
        connection = None
        try:
            connection = pika.BlockingConnection(_connection_parameters(self.worker.settings))
            self._connection = connection
            version = _server_version(connection)
            if version != self.expected_broker_version:
                raise RuntimeError("RabbitMQ server version differs from the proof pin")
            channel = connection.channel()
            self._channel = channel
            proxy = _InstrumentedChannel(
                channel,
                recorder=self.recorder,
                phase=self.phase,
                stop_after_archive_ack=self.stop_after_archive_ack,
                stop_after_next_ack=self.stop_after_next_ack,
            )
            self.worker._declare_topology(proxy)
            channel.basic_qos(prefetch_count=1)

            def consume(_channel, method, properties, body: bytes) -> None:
                headers = getattr(properties, "headers", None)
                attempt = self.worker._attempt(properties, body)
                self.recorder.add(
                    "delivery_received",
                    phase=self.phase,
                    delivery_tag=int(method.delivery_tag),
                    attempt=attempt,
                    message_size=len(body),
                    message_fingerprint=self.worker._message_fingerprint(body),
                    retry_death=_safe_death_proof(
                        headers,
                        self.worker.settings.document_retry_queue,
                    ),
                )
                self.worker._consume(proxy, method, properties, body)

            channel.basic_consume(
                queue=self.worker.settings.document_queue,
                on_message_callback=consume,
                auto_ack=False,
            )
            self.recorder.add(
                "consumer_ready",
                phase=self.phase,
                queue=self.worker.settings.document_queue,
                prefetch=1,
                broker_version=version,
            )
            self.ready.set()
            channel.start_consuming()
        except BaseException as exc:  # retained and reported only by class
            self._error = exc
            self.ready.set()
        finally:
            try:
                if connection is not None and connection.is_open:
                    connection.close()
            except Exception:
                pass

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(timeout=8):
            raise RuntimeError("document proof consumer did not become ready")
        if self._error is not None:
            raise RuntimeError("document proof consumer failed before readiness") from self._error

    def join(self, *, timeout: float) -> None:
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self.stop()
            self._thread.join(timeout=3)
            raise RuntimeError("document proof consumer exceeded its deadline")
        if self._error is not None:
            raise RuntimeError("document proof consumer failed") from self._error

    def stop(self) -> None:
        connection = self._connection
        channel = self._channel
        if connection is None or channel is None:
            return
        try:
            if connection.is_open and channel.is_open:
                connection.add_callback_threadsafe(channel.stop_consuming)
        except Exception:
            pass


class _CallbackHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address,
        *,
        callback_token: str,
        recorder: _EventRecorder,
    ) -> None:
        super().__init__(address, _CallbackHandler)
        self.callback_token = callback_token
        self.recorder = recorder
        self.callbacks: list[dict[str, object]] = []
        self.callbacks_lock = threading.Lock()


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _CallbackHTTPServer

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        try:
            length_value = self.headers.get("Content-Length", "")
            if (
                self.path != EXPECTED_CALLBACK_PATH
                or not length_value.isascii()
                or not length_value.isdigit()
                or not 1 <= int(length_value) <= MAX_CALLBACK_BYTES
                or not hmac.compare_digest(
                    self.headers.get("X-Tryscode-Service", "").encode("utf-8"),
                    self.server.callback_token.encode("utf-8"),
                )
            ):
                raise ValueError("invalid callback request")
            body = self.rfile.read(int(length_value))
            payload = _decode_json_object(body)
            status = payload.get("status")
            if status == "failed":
                if set(payload) != {"ext_id", "status", "progress", "error_message"}:
                    raise ValueError("invalid failure callback")
                if (
                    payload.get("progress") != 0
                    or payload.get("error_message") != SAFE_FAILURE_CODE
                ):
                    raise ValueError("invalid failure callback")
            elif status == "done":
                required = {
                    "callback_version",
                    "ext_id",
                    "status",
                    "progress",
                    "result_path",
                    "result_size_bytes",
                    "result_sha256",
                }
                if not required.issubset(payload) or set(payload) - (
                    required | {"result_storage_version"}
                ):
                    raise ValueError("invalid success callback")
                if payload.get("progress") != 100:
                    raise ValueError("invalid success callback")
            else:
                raise ValueError("invalid callback status")
            with self.server.callbacks_lock:
                self.server.callbacks.append(payload)
            self.server.recorder.add(
                "callback_received",
                phase="failure" if status == "failed" else "replay",
                status=status,
                job_id=payload.get("ext_id"),
                error_code=payload.get("error_message"),
                result_path=payload.get("result_path"),
            )
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (UnicodeError, ValueError, OSError):
            self.server.recorder.add("callback_rejected", phase="callback")
            self.send_response(400)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _CallbackRecorder:
    def __init__(self, token: str, recorder: _EventRecorder) -> None:
        self._server = _CallbackHTTPServer(
            ("127.0.0.1", 0),
            callback_token=token,
            recorder=recorder,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="document-archive-proof-callback",
            daemon=True,
        )

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}{EXPECTED_CALLBACK_PATH}"

    def start(self) -> None:
        self._thread.start()

    def snapshot(self) -> list[dict[str, object]]:
        with self._server.callbacks_lock:
            return [dict(payload) for payload in self._server.callbacks]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3)


def _publish_source(
    settings: Settings,
    *,
    recorder: _EventRecorder,
    phase: str,
    body: bytes,
    message_id: str,
    headers: dict[str, object],
    expected_broker_version: str,
) -> None:
    connection = pika.BlockingConnection(_connection_parameters(settings))
    try:
        version = _server_version(connection)
        if version != expected_broker_version:
            raise RuntimeError("RabbitMQ server version differs from the proof pin")
        channel = connection.channel()
        channel.confirm_delivery()
        recorder.add(
            "source_publish_begin",
            phase=phase,
            queue=settings.document_queue,
            message_id=message_id,
            message_size=len(body),
            message_fingerprint=DocumentWorker._message_fingerprint(body),
        )
        result = channel.basic_publish(
            exchange="",
            routing_key=settings.document_queue,
            body=body,
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,
                message_id=message_id,
                headers=headers,
            ),
            mandatory=True,
        )
        if result is False:
            raise RuntimeError("RabbitMQ negatively acknowledged the source publication")
        recorder.add(
            "source_publish_confirmed",
            phase=phase,
            queue=settings.document_queue,
            message_id=message_id,
        )
    finally:
        if connection.is_open:
            connection.close()


def _validate_archive(
    *,
    archive_body: bytes,
    archive_headers: object,
    source_body: bytes,
    max_retries: int,
) -> dict[str, object]:
    archive = _decode_json_object(archive_body)
    expected = {
        "archive_version": ARCHIVE_VERSION,
        "attempts": max_retries + 1,
        "failure_code": "processing-failed",
        "message_digest": DocumentWorker._message_fingerprint(source_body),
        "message_size": len(source_body),
    }
    if archive != expected:
        raise AssertionError("archive envelope differs from the bounded V1 contract")
    if not isinstance(archive_headers, dict):
        raise AssertionError("archive headers are missing")
    normalized_headers = {
        str(name): _normal_text(value) or value for name, value in archive_headers.items()
    }
    if normalized_headers != {
        "x-tryscode-document-archive-version": ARCHIVE_VERSION,
        "x-tryscode-document-archive-reason": "processing-failed",
    }:
        raise AssertionError("archive headers differ from the bounded V1 contract")
    return archive


def _take_archive(
    settings: Settings,
    *,
    source_body: bytes,
    expected_broker_version: str,
) -> tuple[dict[str, object], dict[str, object]]:
    connection = pika.BlockingConnection(_connection_parameters(settings))
    try:
        if _server_version(connection) != expected_broker_version:
            raise RuntimeError("RabbitMQ server version differs from the proof pin")
        channel = connection.channel()
        method = properties = body = None
        for _attempt in range(50):
            method, properties, body = channel.basic_get(
                queue=settings.document_archive_queue,
                auto_ack=False,
            )
            if method is not None:
                break
            time.sleep(0.1)
        if method is None or properties is None or not isinstance(body, bytes):
            raise AssertionError("archive message was not present in RabbitMQ")
        if properties.content_type != "application/json" or properties.delivery_mode != 2:
            raise AssertionError("archive message is not persistent JSON")
        archive = _validate_archive(
            archive_body=body,
            archive_headers=properties.headers,
            source_body=source_body,
            max_retries=settings.document_max_retries,
        )
        metadata = {
            "content_type": properties.content_type,
            "delivery_mode": properties.delivery_mode,
            "body_size": len(body),
        }
        channel.basic_ack(method.delivery_tag)
        return archive, metadata
    finally:
        if connection.is_open:
            connection.close()


def _queue_depths(settings: Settings) -> dict[str, dict[str, int]]:
    connection = pika.BlockingConnection(_connection_parameters(settings))
    try:
        channel = connection.channel()
        result = {}
        for queue in (
            settings.document_queue,
            settings.document_retry_queue,
            settings.document_archive_queue,
        ):
            declared = channel.queue_declare(queue=queue, passive=True)
            result[queue] = {
                "messages": int(declared.method.message_count),
                "consumers": int(declared.method.consumer_count),
            }
        return result
    finally:
        if connection.is_open:
            connection.close()


def _validate_failure_sequence(events: list[dict[str, object]], *, max_retries: int) -> None:
    phase_events = [event for event in events if event.get("phase") == "failure"]
    deliveries = [event for event in phase_events if event.get("event") == "delivery_received"]
    if [event.get("attempt") for event in deliveries] != list(range(max_retries + 1)):
        raise AssertionError("RabbitMQ deliveries did not traverse every retry attempt")
    for delivery in deliveries[1:]:
        proof = delivery.get("retry_death")
        if not isinstance(proof, dict) or proof.get("reason") != "expired":
            raise AssertionError("retry delivery lacks RabbitMQ x-death expiry proof")

    publications = [event for event in phase_events if event.get("event") == "publish_confirmed"]
    expected_queues = ["document_tasks.retry"] * max_retries + ["document_tasks.archive"]
    if [event.get("queue") for event in publications] != expected_queues:
        raise AssertionError("retry/archive publications are incomplete or out of order")
    acknowledgements = [
        event for event in phase_events if event.get("event") == "source_ack_succeeded"
    ]
    if len(acknowledgements) != max_retries + 1:
        raise AssertionError("not every source delivery was acknowledged")

    for index, delivery in enumerate(deliveries):
        next_sequence = (
            int(deliveries[index + 1]["sequence"]) if index + 1 < len(deliveries) else 10**9
        )
        segment = [
            event
            for event in phase_events
            if int(delivery["sequence"]) < int(event["sequence"]) < next_sequence
        ]
        confirmed = next(
            (event for event in segment if event.get("event") == "publish_confirmed"),
            None,
        )
        acked = next(
            (event for event in segment if event.get("event") == "source_ack_succeeded"),
            None,
        )
        if (
            confirmed is None
            or acked is None
            or int(confirmed["sequence"]) >= int(acked["sequence"])
        ):
            raise AssertionError("source ACK did not follow a positive publisher confirm")

    archive_confirmed = next(
        event
        for event in phase_events
        if event.get("event") == "publish_confirmed"
        and event.get("queue") == "document_tasks.archive"
    )
    failure_callback = next(
        event
        for event in phase_events
        if event.get("event") == "callback_received" and event.get("status") == "failed"
    )
    final_ack = acknowledgements[-1]
    if not (
        int(archive_confirmed["sequence"])
        < int(failure_callback["sequence"])
        < int(final_ack["sequence"])
    ):
        raise AssertionError("archive confirm, terminal callback and ACK are out of order")


def _settings(
    *,
    rabbitmq_url: str,
    signing_key: str,
    callback_url: str,
    callback_token: str,
    output_dir: Path,
) -> Settings:
    settings = Settings(
        rabbitmq_url=rabbitmq_url,
        rabbitmq_socket_timeout_seconds=3,
        rabbitmq_stack_timeout_seconds=5,
        rabbitmq_blocked_timeout_seconds=3,
        rabbitmq_heartbeat_seconds=10,
        document_queue="document_tasks",
        document_retry_queue="document_tasks.retry",
        document_archive_queue="document_tasks.archive",
        document_max_retries=2,
        document_retry_delay_seconds=1,
        document_retry_signing_key=signing_key,
        document_retry_max_messages=20,
        document_retry_max_bytes=10 * 1024 * 1024,
        document_archive_ttl_ms=60_000,
        document_archive_max_messages=10,
        document_output_dir=output_dir,
        document_storage_mode="local",
        document_storage_prefix="documents",
        harmony_callback_url=callback_url,
        harmony_service_token=callback_token,
        document_callback_timeout_seconds=2,
    )
    validate_settings(settings)
    return settings


def _source_payload(job_id: str) -> dict[str, object]:
    return {
        "contract_version": "tryscode.document-render.v1",
        "action": "render_subject_pdf",
        "job_id": job_id,
        "subject_code": "document-archive-proof",
        "title": PEDAGOGICAL_SENTINEL,
        "author": "Baptiste RENNESON BOUTARD",
        "sections": [
            {
                "heading": "Disposable proof content",
                "body": f"{PEDAGOGICAL_SENTINEL} {FAKE_SECRET_SENTINEL}",
            }
        ],
    }


def _encoded_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")


def _run_proof() -> dict[str, object]:
    rabbitmq_url = _required_environment("RABBITMQ_URL")
    signing_key = _required_environment("DOCUMENT_RETRY_SIGNING_KEY")
    callback_token = _required_environment("HARMONY_SERVICE_TOKEN")
    replay_confirmation = _required_environment("DOCUMENT_ARCHIVE_PROOF_CONFIRMATION")
    expected_broker_version = _required_environment("RABBITMQ_EXPECTED_VERSION")
    _assert_isolated_configuration(
        rabbitmq_url=rabbitmq_url,
        signing_key=signing_key,
        callback_token=callback_token,
        replay_confirmation=replay_confirmation,
        expected_broker_version=expected_broker_version,
    )

    recorder = _EventRecorder()
    callbacks = _CallbackRecorder(callback_token, recorder)
    callbacks.start()
    worker_logger = logging.getLogger("doc_worker.worker")
    log_output = io.StringIO()
    log_handler = logging.StreamHandler(log_output)
    log_handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    previous_level = worker_logger.level
    previous_propagate = worker_logger.propagate
    worker_logger.setLevel(logging.INFO)
    worker_logger.propagate = False
    worker_logger.addHandler(log_handler)

    failure_phase: _WorkerPhase | None = None
    replay_phase: _WorkerPhase | None = None
    try:
        failure_settings = _settings(
            rabbitmq_url=rabbitmq_url,
            signing_key=signing_key,
            callback_url=callbacks.url,
            callback_token=callback_token,
            output_dir=Path("/proc/tryscode-document-archive-proof"),
        )
        source_body = _encoded_payload(_source_payload(PROOF_JOB_ID))
        source_fingerprint = DocumentWorker._message_fingerprint(source_body)
        failure_phase = _WorkerPhase(
            DocumentWorker(failure_settings),
            recorder=recorder,
            phase="failure",
            expected_broker_version=expected_broker_version,
            stop_after_archive_ack=True,
        )
        failure_phase.start()
        _publish_source(
            failure_settings,
            recorder=recorder,
            phase="failure",
            body=source_body,
            message_id=PROOF_JOB_ID,
            headers={"x-tryscode-proof-input": HEADER_SENTINEL},
            expected_broker_version=expected_broker_version,
        )
        failure_phase.join(timeout=20)

        callback_snapshot = callbacks.snapshot()
        if callback_snapshot != [
            {
                "ext_id": PROOF_JOB_ID,
                "status": "failed",
                "progress": 0,
                "error_message": SAFE_FAILURE_CODE,
            }
        ]:
            raise AssertionError("terminal failure callback is missing or contains extra data")

        archive, archive_metadata = _take_archive(
            failure_settings,
            source_body=source_body,
            expected_broker_version=expected_broker_version,
        )
        if archive["message_digest"] != source_fingerprint:
            raise AssertionError("archive cannot be correlated to the retained source")

        failure_depths = _queue_depths(failure_settings)
        if any(entry["messages"] != 0 for entry in failure_depths.values()):
            raise AssertionError("document queues were not drained after archive inspection")

        # The archive contains no replay body. A controlled operator must use
        # an authoritative retained source and create a fresh job identity.
        recorder.add(
            "controlled_replay_authorized",
            phase="replay",
            source_archive_digest_matches=True,
            old_job_id=PROOF_JOB_ID,
            new_job_id=REPLAY_JOB_ID,
        )
        replay_body = _encoded_payload(_source_payload(REPLAY_JOB_ID))
        with tempfile.TemporaryDirectory(prefix="tryscode-document-archive-replay-") as output:
            output_dir = Path(output)
            replay_settings = _settings(
                rabbitmq_url=rabbitmq_url,
                signing_key=signing_key,
                callback_url=callbacks.url,
                callback_token=callback_token,
                output_dir=output_dir,
            )
            replay_phase = _WorkerPhase(
                DocumentWorker(replay_settings),
                recorder=recorder,
                phase="replay",
                expected_broker_version=expected_broker_version,
                stop_after_next_ack=True,
            )
            replay_phase.start()
            _publish_source(
                replay_settings,
                recorder=recorder,
                phase="replay",
                body=replay_body,
                message_id=REPLAY_JOB_ID,
                headers={"x-tryscode-proof-controlled-replay": "authorized"},
                expected_broker_version=expected_broker_version,
            )
            replay_phase.join(timeout=12)

            callback_snapshot = callbacks.snapshot()
            if len(callback_snapshot) != 2 or callback_snapshot[1].get("status") != "done":
                raise AssertionError("controlled replay did not produce a success callback")
            success = callback_snapshot[1]
            if success.get("ext_id") != REPLAY_JOB_ID:
                raise AssertionError("controlled replay reused the failed job identity")
            result_path = success.get("result_path")
            if (
                not isinstance(result_path, str)
                or not result_path.startswith("documents/")
                or "/" in result_path[len("documents/") :]
            ):
                raise AssertionError("controlled replay returned an unsafe result path")
            rendered = output_dir / Path(result_path).name
            rendered_bytes = rendered.read_bytes()
            if not rendered_bytes.startswith(b"%PDF-"):
                raise AssertionError("controlled replay did not render a PDF")
            if success.get("result_size_bytes") != len(rendered_bytes):
                raise AssertionError("controlled replay size callback differs from the PDF")
            if success.get("result_sha256") != hashlib.sha256(rendered_bytes).hexdigest():
                raise AssertionError("controlled replay digest callback differs from the PDF")

            final_depths = _queue_depths(replay_settings)
            if any(entry["messages"] != 0 for entry in final_depths.values()):
                raise AssertionError("document queues were not empty after controlled replay")

        events = recorder.snapshot()
        _validate_failure_sequence(events, max_retries=failure_settings.document_max_retries)
        replay_delivery = [
            event
            for event in events
            if event.get("phase") == "replay" and event.get("event") == "delivery_received"
        ]
        replay_ack = [
            event
            for event in events
            if event.get("phase") == "replay" and event.get("event") == "source_ack_succeeded"
        ]
        replay_callbacks = [
            event
            for event in events
            if event.get("phase") == "replay"
            and event.get("event") == "callback_received"
            and event.get("status") == "done"
        ]
        if not (
            len(replay_delivery) == len(replay_ack) == len(replay_callbacks) == 1
            and int(replay_callbacks[0]["sequence"]) < int(replay_ack[0]["sequence"])
        ):
            raise AssertionError("controlled replay callback/ACK sequence is invalid")

        log_handler.flush()
        safe_logs = [line for line in log_output.getvalue().splitlines() if line]
        expected_log_fragments = (
            "retry 1/2",
            "retry 2/2",
            "Archived document task after 2 retries",
            f"Rendered document job {REPLAY_JOB_ID}",
        )
        if not all(
            any(fragment in line for line in safe_logs) for fragment in expected_log_fragments
        ):
            raise AssertionError("worker logs do not prove the complete bounded lifecycle")

        archive_serialized = json.dumps(archive, sort_keys=True)
        evidence_serialized = json.dumps(
            {"archive": archive, "events": events, "logs": safe_logs},
            ensure_ascii=True,
            sort_keys=True,
        )
        forbidden_values = (
            PEDAGOGICAL_SENTINEL,
            FAKE_SECRET_SENTINEL,
            HEADER_SENTINEL,
            signing_key,
            callback_token,
            rabbitmq_url,
        )
        if any(value in archive_serialized for value in forbidden_values):
            raise AssertionError("archive contains document content or a proof credential")
        if any(value in evidence_serialized for value in forbidden_values):
            raise AssertionError("persistable proof evidence contains sensitive test input")

        retry_publications = [
            event
            for event in events
            if event.get("phase") == "failure"
            and event.get("event") == "publish_confirmed"
            and event.get("queue") == "document_tasks.retry"
        ]
        source_acks = [
            event
            for event in events
            if event.get("phase") == "failure" and event.get("event") == "source_ack_succeeded"
        ]
        topology = [
            event
            for event in events
            if event.get("phase") == "failure" and event.get("event") == "queue_declare_begin"
        ]
        return {
            "proof": PROOF_VERSION,
            "status": "partial",
            "environment": {
                "rabbitmq_version": expected_broker_version,
                "pika_version": pika.__version__,
                "worker_storage_failure": "unwritable disposable /proc path",
                "max_retries": failure_settings.document_max_retries,
                "retry_delay_seconds": failure_settings.document_retry_delay_seconds,
            },
            "archive": archive,
            "archive_message": archive_metadata,
            "topology": topology,
            "timeline": events,
            "worker_logs": safe_logs,
            "queue_depths_after_archive_inspection": failure_depths,
            "queue_depths_after_controlled_replay": final_depths,
            "assertions": {
                "real_broker_message_received": True,
                "successive_failures_observed": True,
                "real_quorum_retries_observed": len(retry_publications) == 2,
                "maximum_attempt_count_reached": archive["attempts"] == 3,
                "archive_published": True,
                "publisher_confirm_before_every_source_ack": len(source_acks) == 3,
                "initial_and_retry_deliveries_acked": len(source_acks) == 3,
                "archive_contract_is_exact_and_bounded": True,
                "archive_has_no_complete_pedagogical_document": True,
                "archive_and_evidence_have_no_proof_secret": True,
                "diagnostic_digest_matches_retained_source": True,
                "worker_level_controlled_replay_succeeded": True,
                "controlled_replay_created_fresh_job_identity": True,
            },
            "criteria": {
                "173-01": {"status": "passed", "evidence": ["timeline"]},
                "173-02": {"status": "passed", "evidence": ["worker_logs", "timeline"]},
                "173-03": {"status": "passed", "evidence": ["topology", "timeline"]},
                "173-04": {"status": "passed", "evidence": ["archive", "worker_logs"]},
                "173-05": {"status": "passed", "evidence": ["archive", "timeline"]},
                "173-06": {"status": "passed", "evidence": ["timeline"]},
                "173-07": {"status": "passed", "evidence": ["timeline"]},
                "173-08": {"status": "passed", "evidence": ["archive"]},
                "173-09": {"status": "passed", "evidence": ["archive", "assertions"]},
                "173-10": {"status": "passed", "evidence": ["archive", "assertions"]},
                "173-11": {
                    "status": "not_run",
                    "evidence": [],
                    "reason": "Sauron and Harmony are outside the DocGenerator worker proof boundary",
                },
                "173-12": {"status": "passed", "evidence": ["archive", "worker_logs"]},
                "173-13": {
                    "status": "partial",
                    "evidence": ["timeline", "assertions"],
                    "reason": "worker-level replay is proven; Sauron authorization and Harmony outbox regeneration are not",
                },
            },
            "limits": [
                "Disposable local RabbitMQ proof; no staging or production claim.",
                "The callback receiver validates the worker contract but is not Harmony.",
                "Sauron visibility and its human authorization path remain not run.",
                "Controlled replay uses retained test data and a fresh job id; a product replay must originate from Harmony's authoritative outbox.",
            ],
            "cleanup": {
                "archive_consumed_after_inspection": True,
                "rendered_replay_temporary_directory_deleted": True,
            },
        }
    finally:
        if failure_phase is not None:
            failure_phase.stop()
        if replay_phase is not None:
            replay_phase.stop()
        worker_logger.removeHandler(log_handler)
        worker_logger.setLevel(previous_level)
        worker_logger.propagate = previous_propagate
        callbacks.close()


def main() -> None:
    try:
        result = _run_proof()
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "proof": PROOF_VERSION,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    main()
