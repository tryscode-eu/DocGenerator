"""RabbitMQ listener with delayed retry and archive queues for document tasks."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from pathlib import Path

import pika

from .config import Settings
from .notifier import notify_failure, notify_success
from .renderer import DocumentValidationError, parse_document, render_document
from .storage import publish_document

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 5 * 1024 * 1024
MAX_CALLBACK_STATE_BYTES = 16 * 1024
ARCHIVE_VERSION = "tryscode.document-archive.v1"
CALLBACK_STATE_VERSION = "tryscode.document-callback-state.v1"
ATTEMPT_HEADER = "x-tryscode-document-attempt"
CALLBACK_PENDING_HEADER = "x-tryscode-document-callback-pending"
RETRY_SIGNATURE_HEADER = "x-tryscode-document-retry-signature"
RETRY_STATE_VERSION_HEADER = "x-tryscode-document-retry-state-version"
RETRY_STATE_VERSION = "v1"
ARCHIVE_FAILURE_CODES = frozenset(
    {
        "message-too-large",
        "invalid-json",
        "invalid-document",
        "processing-failed",
        "callback-failed",
        "invalid-retry-state",
    }
)


class PermanentDocumentError(ValueError):
    """A bounded, non-sensitive reason for archiving without another retry."""

    def __init__(self, failure_code: str):
        if failure_code not in ARCHIVE_FAILURE_CODES - {"processing-failed"}:
            raise ValueError("invalid permanent document failure code")
        self.failure_code = failure_code
        super().__init__(failure_code)


class WorkerReconnectRequired(RuntimeError):
    """Stop this consumer connection and apply the outer reconnect backoff."""


class PendingCallbackError(RuntimeError):
    """Carry a compact callback continuation without document source data."""

    def __init__(self, body: bytes, mode: str):
        self.body = body
        self.mode = mode
        super().__init__("document callback pending")


class InvalidRetryStateError(ValueError):
    """A producer supplied unauthenticated worker-internal retry metadata."""


class DocumentWorker:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _publish_confirmed(
        self,
        channel,
        queue: str,
        body: bytes,
        headers: dict,
        *,
        delay_ms: int | None = None,
    ) -> None:
        properties = pika.BasicProperties(
            content_type="application/json",
            delivery_mode=2,
            headers=headers,
            expiration=str(delay_ms) if delay_ms else None,
        )
        # In Pika confirm mode success normally returns None. Any NACK,
        # unroutable mandatory message or connection failure raises instead.
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=body,
            properties=properties,
            mandatory=True,
        )

    def _retry_signature(self, body: bytes, *, attempt: int, mode: str | None) -> str:
        key = self.settings.document_retry_signing_key.encode("utf-8", errors="strict")
        state = b"\x00".join(
            (
                RETRY_STATE_VERSION.encode("ascii"),
                str(attempt).encode("ascii"),
                (mode or "").encode("ascii"),
                body,
            )
        )
        return hmac.new(key, state, hashlib.sha256).hexdigest()

    def _retry_headers(
        self,
        body: bytes,
        *,
        attempt: int,
        mode: str | None = None,
    ) -> dict[str, object]:
        if mode not in {None, "success", "failure"}:
            raise ValueError("invalid callback retry mode")
        headers: dict[str, object] = {
            ATTEMPT_HEADER: attempt,
            RETRY_STATE_VERSION_HEADER: RETRY_STATE_VERSION,
        }
        if mode is not None:
            headers[CALLBACK_PENDING_HEADER] = mode
        headers[RETRY_SIGNATURE_HEADER] = self._retry_signature(
            body,
            attempt=attempt,
            mode=mode,
        )
        return headers

    def _attempt(self, properties, body: bytes) -> int:
        if len(body) > MAX_MESSAGE_BYTES:
            return 0
        headers = getattr(properties, "headers", None)
        if not isinstance(headers, dict):
            return 0
        value = headers.get(ATTEMPT_HEADER)
        if type(value) is not int or not 1 <= value <= self.settings.document_max_retries:
            return 0
        mode = headers.get(CALLBACK_PENDING_HEADER)
        if mode is not None and (not isinstance(mode, str) or mode not in ("success", "failure")):
            return 0
        if mode is not None and len(body) > MAX_CALLBACK_STATE_BYTES:
            return 0
        signature = headers.get(RETRY_SIGNATURE_HEADER)
        if (
            headers.get(RETRY_STATE_VERSION_HEADER) != RETRY_STATE_VERSION
            or not isinstance(signature, str)
            or not re.fullmatch(r"[0-9a-f]{64}", signature)
            or not hmac.compare_digest(
                signature,
                self._retry_signature(body, attempt=value, mode=mode),
            )
        ):
            return 0
        deaths = headers.get("x-death")
        if not isinstance(deaths, list):
            return 0
        for death in deaths[:16]:
            if not isinstance(death, dict):
                continue
            count = death.get("count")
            if (
                death.get("queue") == self.settings.document_retry_queue
                and death.get("reason") == "expired"
                and type(count) is int
                and count >= 1
            ):
                return value
        return 0

    def _callback_mode(self, properties, body: bytes) -> str | None:
        headers = getattr(properties, "headers", None)
        internal = {
            ATTEMPT_HEADER,
            CALLBACK_PENDING_HEADER,
            RETRY_SIGNATURE_HEADER,
            RETRY_STATE_VERSION_HEADER,
        }
        if self._attempt(properties, body) < 1:
            if isinstance(headers, dict) and internal.intersection(headers):
                raise InvalidRetryStateError("invalid authenticated retry state")
            return None
        assert isinstance(headers, dict)
        mode = headers.get(CALLBACK_PENDING_HEADER)
        return mode if mode in {"success", "failure"} else None

    @staticmethod
    def _message_fingerprint(body: bytes) -> str:
        """Hash bounded input even when RabbitMQ already delivered a huge body."""

        prefix = body[:MAX_MESSAGE_BYTES]
        digest_input = str(len(body)).encode("ascii") + b":" + prefix
        return hashlib.sha256(digest_input).hexdigest()

    def _archive_body(
        self,
        body: bytes,
        *,
        attempts: int,
        failure_code: str,
    ) -> bytes:
        if failure_code not in ARCHIVE_FAILURE_CODES:
            raise ValueError("invalid archive failure code")
        envelope = {
            "archive_version": ARCHIVE_VERSION,
            "attempts": min(
                max(1, int(attempts)),
                self.settings.document_max_retries + 1,
            ),
            "failure_code": failure_code,
            "message_digest": self._message_fingerprint(body),
            "message_size": len(body),
        }
        return json.dumps(
            envelope,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")

    def _publish_archive(
        self,
        channel,
        body: bytes,
        *,
        attempts: int,
        failure_code: str,
    ) -> None:
        archive_body = self._archive_body(
            body,
            attempts=attempts,
            failure_code=failure_code,
        )
        self._publish_confirmed(
            channel,
            self.settings.document_archive_queue,
            archive_body,
            {
                "x-tryscode-document-archive-version": ARCHIVE_VERSION,
                "x-tryscode-document-archive-reason": failure_code,
            },
        )

    @staticmethod
    def _requeue_and_stop(channel, delivery_tag) -> None:
        """Requeue the source, then force a clean channel reconnect."""

        nack_error = None
        try:
            channel.basic_nack(delivery_tag, requeue=True)
        except Exception as exc:  # the outer loop must reconnect a closed channel
            nack_error = exc
        stop_error = None
        try:
            channel.stop_consuming()
        except Exception as exc:
            stop_error = exc
        reconnect_error = nack_error or stop_error
        if reconnect_error is not None:
            raise WorkerReconnectRequired(
                "document consumer reconnect required"
            ) from reconnect_error
        raise WorkerReconnectRequired("document consumer reconnect required")

    @staticmethod
    def _ack_or_reconnect(channel, delivery_tag) -> None:
        """Never reinterpret an ambiguous ACK as a processing failure."""

        try:
            channel.basic_ack(delivery_tag)
        except Exception as exc:
            try:
                channel.stop_consuming()
            except Exception:
                pass
            raise WorkerReconnectRequired("document acknowledgement requires reconnect") from exc

    @staticmethod
    def _callback_body(
        *,
        mode: str,
        job_id: str,
        result_path: str | None = None,
        result_size_bytes: int | None = None,
        result_sha256: str | None = None,
        result_storage_version: str | None = None,
        failure_code: str | None = None,
    ) -> bytes:
        if mode == "success":
            payload = {
                "callback_state_version": CALLBACK_STATE_VERSION,
                "mode": "success",
                "job_id": job_id,
                "result_path": result_path,
                "result_size_bytes": result_size_bytes,
                "result_sha256": result_sha256,
                "result_storage_version": result_storage_version,
            }
        elif mode == "failure":
            payload = {
                "callback_state_version": CALLBACK_STATE_VERSION,
                "mode": "failure",
                "job_id": job_id,
                "failure_code": failure_code,
            }
        else:
            raise ValueError("invalid callback state mode")
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        if len(encoded) > MAX_CALLBACK_STATE_BYTES:
            raise RuntimeError("document callback state is too large")
        return encoded

    @staticmethod
    def _decode_callback_body(body: bytes, mode: str) -> dict[str, object]:
        if not body or len(body) > MAX_CALLBACK_STATE_BYTES:
            raise ValueError("invalid document callback state")

        def reject_duplicates(pairs):
            value = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError("duplicate document callback state key")
                value[key] = item
            return value

        try:
            payload = json.loads(
                body.decode("ascii"),
                object_pairs_hook=reject_duplicates,
            )
        except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise ValueError("invalid document callback state") from None
        success_keys = {
            "callback_state_version",
            "mode",
            "job_id",
            "result_path",
            "result_size_bytes",
            "result_sha256",
            "result_storage_version",
        }
        failure_keys = {
            "callback_state_version",
            "mode",
            "job_id",
            "failure_code",
        }
        expected = success_keys if mode == "success" else failure_keys
        if (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("callback_state_version") != CALLBACK_STATE_VERSION
            or payload.get("mode") != mode
        ):
            raise ValueError("invalid document callback state")
        return payload

    def _deliver_callback(self, body: bytes, mode: str) -> None:
        payload = self._decode_callback_body(body, mode)
        if mode == "success":
            notify_success(
                self.settings,
                job_id=payload["job_id"],
                result_path=payload["result_path"],
                result_size_bytes=payload["result_size_bytes"],
                result_sha256=payload["result_sha256"],
                result_storage_version=payload["result_storage_version"],
            )
        else:
            notify_failure(
                self.settings,
                job_id=payload["job_id"],
                failure_code=payload["failure_code"],
            )

    def _handle(self, body: bytes) -> str:
        if len(body) > MAX_MESSAGE_BYTES:
            raise PermanentDocumentError("message-too-large")
        if not body:
            raise PermanentDocumentError("invalid-json")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            raise PermanentDocumentError("invalid-json") from None
        try:
            document_data = parse_document(payload)
        except (DocumentValidationError, RecursionError):
            raise PermanentDocumentError("invalid-document") from None
        path = render_document(document_data, self.settings.document_output_dir)
        published = publish_document(self.settings, path)
        if self.settings.document_storage_mode.lower() == "s3":
            # The object identity is already durable and idempotent. Remove the
            # local staging copy before telling Harmony that the job is done so
            # a cleanup failure can never follow a terminal success callback.
            Path(path).unlink(missing_ok=True)
        callback_body = self._callback_body(
            mode="success",
            job_id=document_data.job_id,
            result_path=published.storage_key,
            result_size_bytes=published.size_bytes,
            result_sha256=published.sha256,
            result_storage_version=published.storage_version,
        )
        try:
            self._deliver_callback(callback_body, "success")
        except RuntimeError as exc:
            raise PendingCallbackError(callback_body, "success") from exc
        logger.info("Rendered document job %s (%s)", document_data.job_id, payload["action"])
        return str(path)

    @staticmethod
    def _trusted_job_id(body: bytes) -> str | None:
        """Extract an identity only from a complete, strictly valid document request."""

        if not body or len(body) > MAX_MESSAGE_BYTES:
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
            return parse_document(payload).job_id
        except (UnicodeDecodeError, json.JSONDecodeError, DocumentValidationError, RecursionError):
            return None

    def _publish_retry(
        self,
        channel,
        body: bytes,
        *,
        attempt: int,
        mode: str | None = None,
    ) -> None:
        self._publish_confirmed(
            channel,
            self.settings.document_retry_queue,
            body,
            self._retry_headers(body, attempt=attempt, mode=mode),
            delay_ms=(self.settings.document_retry_delay_seconds * 1000),
        )

    def _settle_callback_failure(
        self,
        channel,
        delivery_tag,
        body: bytes,
        *,
        mode: str,
        attempt: int,
    ) -> None:
        try:
            if attempt < self.settings.document_max_retries:
                self._publish_retry(
                    channel,
                    body,
                    attempt=attempt + 1,
                    mode=mode,
                )
                logger.warning(
                    "Deferred document callback retry %d/%d",
                    attempt + 1,
                    self.settings.document_max_retries,
                )
            else:
                self._publish_archive(
                    channel,
                    body,
                    attempts=attempt + 1,
                    failure_code="callback-failed",
                )
                logger.error("Document callback exhausted its bounded retries")
        except Exception:
            self._requeue_and_stop(channel, delivery_tag)
        self._ack_or_reconnect(channel, delivery_tag)

    def _settle_processing_failure(
        self,
        channel,
        delivery_tag,
        properties,
        body: bytes,
    ) -> None:
        attempt = self._attempt(properties, body)
        try:
            if attempt >= self.settings.document_max_retries:
                self._publish_archive(
                    channel,
                    body,
                    attempts=attempt + 1,
                    failure_code="processing-failed",
                )
                trusted_job_id = self._trusted_job_id(body)
                if trusted_job_id is not None:
                    try:
                        notify_failure(
                            self.settings,
                            job_id=trusted_job_id,
                            failure_code="document-processing-failed",
                        )
                    except RuntimeError:
                        callback_body = self._callback_body(
                            mode="failure",
                            job_id=trusted_job_id,
                            failure_code="document-processing-failed",
                        )
                        if self.settings.document_max_retries > 0:
                            self._publish_retry(
                                channel,
                                callback_body,
                                attempt=1,
                                mode="failure",
                            )
                        else:
                            self._publish_archive(
                                channel,
                                callback_body,
                                attempts=1,
                                failure_code="callback-failed",
                            )
                logger.warning("Archived document task after %d retries", attempt)
            else:
                self._publish_retry(
                    channel,
                    body,
                    attempt=attempt + 1,
                )
                logger.warning(
                    "Deferred document task for retry %d/%d",
                    attempt + 1,
                    self.settings.document_max_retries,
                )
        except Exception:
            self._requeue_and_stop(channel, delivery_tag)
        self._ack_or_reconnect(channel, delivery_tag)

    def _consume(self, channel, method, properties, body: bytes) -> None:
        delivery_tag = method.delivery_tag
        try:
            callback_mode = self._callback_mode(properties, body)
        except InvalidRetryStateError:
            try:
                self._publish_archive(
                    channel,
                    body,
                    attempts=1,
                    failure_code="invalid-retry-state",
                )
            except Exception:
                self._requeue_and_stop(channel, delivery_tag)
            self._ack_or_reconnect(channel, delivery_tag)
            return

        try:
            if callback_mode is None:
                self._handle(body)
            else:
                self._deliver_callback(body, callback_mode)
        except PendingCallbackError as exc:
            self._settle_callback_failure(
                channel,
                delivery_tag,
                exc.body,
                mode=exc.mode,
                attempt=0,
            )
        except PermanentDocumentError as exc:
            try:
                self._publish_archive(
                    channel,
                    body,
                    attempts=1,
                    failure_code=exc.failure_code,
                )
            except Exception:
                self._requeue_and_stop(channel, delivery_tag)
            self._ack_or_reconnect(channel, delivery_tag)
        except Exception:
            if callback_mode is not None:
                self._settle_callback_failure(
                    channel,
                    delivery_tag,
                    body,
                    mode=callback_mode,
                    attempt=self._attempt(properties, body),
                )
            else:
                self._settle_processing_failure(
                    channel,
                    delivery_tag,
                    properties,
                    body,
                )
        else:
            self._ack_or_reconnect(channel, delivery_tag)

    def _declare_topology(self, channel) -> None:
        channel.queue_declare(queue=self.settings.document_queue, durable=True)
        channel.queue_declare(
            queue=self.settings.document_retry_queue,
            durable=True,
            arguments={
                "x-queue-type": "quorum",
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self.settings.document_queue,
                "x-dead-letter-strategy": "at-least-once",
                "x-overflow": "reject-publish",
                "x-max-length": self.settings.document_retry_max_messages,
                "x-max-length-bytes": self.settings.document_retry_max_bytes,
            },
        )
        channel.queue_declare(
            queue=self.settings.document_archive_queue,
            durable=True,
            arguments={
                "x-message-ttl": self.settings.document_archive_ttl_ms,
                "x-max-length": self.settings.document_archive_max_messages,
                "x-overflow": "drop-head",
            },
        )
        channel.confirm_delivery()

    def start(self) -> None:
        params = pika.URLParameters(self.settings.rabbitmq_url)
        params.socket_timeout = self.settings.rabbitmq_socket_timeout_seconds
        params.stack_timeout = self.settings.rabbitmq_stack_timeout_seconds
        params.blocked_connection_timeout = self.settings.rabbitmq_blocked_timeout_seconds
        params.heartbeat = self.settings.rabbitmq_heartbeat_seconds
        params.connection_attempts = 1
        params.retry_delay = 0
        while True:
            connection = None
            reconnect = False
            try:
                connection = pika.BlockingConnection(params)
                channel = connection.channel()
                self._declare_topology(channel)
                channel.basic_qos(prefetch_count=1)
                channel.basic_consume(
                    queue=self.settings.document_queue,
                    on_message_callback=self._consume,
                )
                logger.info("Document worker listening on %s", self.settings.document_queue)
                channel.start_consuming()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                logger.warning(
                    "Document worker connection lost; retrying (%s)",
                    type(exc).__name__,
                )
                reconnect = True
            finally:
                try:
                    if connection is not None and connection.is_open:
                        connection.close()
                except Exception:
                    pass
            if reconnect:
                time.sleep(3)
