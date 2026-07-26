"""Tests du runtime DocGenerator sans effet externe."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from doc_worker.config import Settings, validate_settings

ROOT = Path(__file__).resolve().parents[1]


def test_safe_idle_requires_no_external_configuration() -> None:
    """Le mode sûr démarre sans broker, callback, secret ou stockage."""

    validate_settings(Settings(_env_file=None, worker_mode="safe_idle"))


def test_active_mode_fails_closed_without_broker() -> None:
    """Le traitement réel exige toujours sa configuration RabbitMQ."""

    with pytest.raises(RuntimeError, match="RABBITMQ_URL est requis"):
        validate_settings(Settings(_env_file=None, worker_mode="active"))


def test_safe_idle_heartbeats_refuses_work_and_stops() -> None:
    """Le processus reste sain, refuse un document puis s'arrête proprement."""

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
    environment = os.environ.copy()
    environment.update(
        {
            "WORKER_MODE": "safe_idle",
            "HEALTH_PORT": str(port),
            "HEARTBEAT_SECONDS": "0.05",
            "SERVICE_COMMIT": "0123456789abcdef",
            "TRYSCODE_ENVIRONMENT": "test",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "doc_worker.main"],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(base + "/health/ready", timeout=0.2) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("le worker n'a pas démarré")

        time.sleep(0.12)
        with urllib.request.urlopen(base + "/health/status", timeout=2) as response:
            status = json.loads(response.read())
        assert status["mode"] == "safe_idle"
        assert status["heartbeat_count"] >= 1

        request = urllib.request.Request(
            base + "/documents",
            method="POST",
            data=b'{"document":"contenu-prive"}',
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(request, timeout=2)
        except urllib.error.HTTPError as response:
            body = response.read().decode()
            assert response.code == 503
        else:
            raise AssertionError("le runtime sûr a accepté un document")
        assert "document_generation_disabled" in body
        assert "contenu-prive" not in body
    finally:
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=10)

    assert process.returncode == 0
    assert "contenu-prive" not in output
    assert "worker arrêté proprement" in output
