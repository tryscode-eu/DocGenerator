import logging

from .config import get_settings, validate_settings
from .worker import DocumentWorker


def main() -> None:
    settings = get_settings()
    validate_settings(settings)
    logging.basicConfig(level=logging.DEBUG if settings.debug else logging.INFO)
    DocumentWorker(settings).start()


if __name__ == "__main__":
    main()
