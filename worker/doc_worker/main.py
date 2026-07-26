"""Sélectionne le consumer de documents ou son runtime sans effet."""

import logging

from .config import get_settings, validate_settings
from .runtime import run_safe_idle
from .worker import DocumentWorker


def main() -> None:
    """Valide la configuration puis démarre exactement le mode demandé."""

    settings = get_settings()
    validate_settings(settings)
    if settings.worker_mode == "safe_idle":
        run_safe_idle(settings)
        return
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
    DocumentWorker(settings).start()


if __name__ == "__main__":
    main()
