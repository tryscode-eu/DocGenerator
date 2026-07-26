"""Maintient DocGenerator vivant sans consommer ni générer de document.

Le mode ``safe_idle`` expose uniquement des sondes internes et un heartbeat
mémoire. Il ne contacte ni RabbitMQ, ni Harmony, ni S3/MinIO et n'écrit aucun
artefact.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import FrameType

from .config import Settings

LOGGER = logging.getLogger("doc_worker")


class JsonFormatter(logging.Formatter):
    """Sérialise les événements techniques en lignes JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Produit un log sans document, destinataire ou identifiant."""

        return json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "service.name": "doc-generator",
                "message": record.getMessage(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


def configure_logging(level: str) -> None:
    """Installe un handler JSON unique pour le runtime sûr."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


@dataclass(slots=True)
class WorkerState:
    """Conserve uniquement l'état technique éphémère des sondes."""

    settings: Settings
    started_monotonic: float = field(default_factory=time.monotonic)
    heartbeat_count: int = 0
    stopping: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def heartbeat(self) -> None:
        """Incrémente le heartbeat sans traiter de document."""

        with self.lock:
            self.heartbeat_count += 1

    def snapshot(self) -> dict[str, object]:
        """Retourne un instantané non sensible et cohérent."""

        with self.lock:
            count = self.heartbeat_count
        return {
            "mode": self.settings.worker_mode,
            "heartbeat_count": count,
            "uptime_seconds": round(time.monotonic() - self.started_monotonic, 3),
        }


def heartbeat_loop(state: WorkerState) -> None:
    """Maintient un heartbeat borné jusqu'à la demande d'arrêt."""

    while not state.stopping.wait(state.settings.heartbeat_seconds):
        state.heartbeat()


def make_handler(state: WorkerState) -> type[BaseHTTPRequestHandler]:
    """Construit le handler des sondes lié à l'état courant."""

    class HealthHandler(BaseHTTPRequestHandler):
        """Expose uniquement santé, version, état et métriques."""

        server_version = ""
        sys_version = ""

        def _json(self, status: HTTPStatus, body: dict[str, object]) -> None:
            """Envoie une réponse JSON compacte sans donnée sensible."""

            encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            """Répond aux sondes reconnues et refuse les autres routes."""

            path = self.path.split("?", maxsplit=1)[0]
            if path == "/health/live":
                self._json(HTTPStatus.OK, {"status": "live"})
                return
            if path == "/health/ready":
                self._json(HTTPStatus.OK, {"status": "ready", "mode": "safe_idle"})
                return
            if path == "/health/version":
                self._json(
                    HTTPStatus.OK,
                    {
                        "service": "doc-generator",
                        "version": state.settings.service_version,
                        "commit": state.settings.service_commit,
                        "environment": state.settings.tryscode_environment,
                    },
                )
                return
            if path == "/health/status":
                self._json(HTTPStatus.OK, state.snapshot())
                return
            if path == "/metrics":
                count = state.snapshot()["heartbeat_count"]
                body = (
                    "# TYPE tryscode_worker_heartbeat_total counter\n"
                    f"tryscode_worker_heartbeat_total {count}\n"
                    "# TYPE tryscode_worker_external_effects gauge\n"
                    "tryscode_worker_external_effects 0\n"
                ).encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._json(HTTPStatus.NOT_FOUND, {"error": {"code": "route_not_found"}})

        def do_POST(self) -> None:  # noqa: N802
            """Refuse tout travail sans lire le corps de la requête."""

            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": {"code": "document_generation_disabled"}},
            )

        def log_message(self, _format: str, *_args: object) -> None:
            """Désactive les access logs susceptibles de contenir une route."""

    return HealthHandler


def run_safe_idle(settings: Settings) -> None:
    """Démarre les sondes et assure un arrêt propre sur SIGTERM/SIGINT.

    Effets de bord:
        Ouvre uniquement le port de santé et écrit des logs techniques. Aucun
        document, message, callback ou objet distant n'est lu ou modifié.
    """

    configure_logging(settings.log_level)
    state = WorkerState(settings)
    heartbeat = threading.Thread(
        target=heartbeat_loop,
        args=(state,),
        name="heartbeat",
        daemon=True,
    )
    server = ThreadingHTTPServer(
        (settings.health_host, settings.health_port),
        make_handler(state),
    )

    def stop(_signum: int, _frame: FrameType | None) -> None:
        """Déclenche l'arrêt hors du handler pour éviter un deadlock."""

        state.stopping.set()
        threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    heartbeat.start()
    LOGGER.info("worker démarré en safe_idle")
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        state.stopping.set()
        heartbeat.join(timeout=settings.heartbeat_seconds + 1)
        server.server_close()
        LOGGER.info("worker arrêté proprement")
