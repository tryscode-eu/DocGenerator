#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PYTHON=${PYTHON:-python3}
VENV=${VENV:-"$ROOT_DIR/.venv"}

case "$VENV" in
    /*) ;;
    *) VENV="$ROOT_DIR/$VENV" ;;
esac

if [ -n "${VIRTUAL_ENV:-}" ]; then
    ENV_PYTHON="$VIRTUAL_ENV/bin/python"
elif [ -x "$VENV/bin/python" ]; then
    ENV_PYTHON="$VENV/bin/python"
else
    if ! "$PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
        printf '%s\n' \
            'DocGenerator requires Python 3.12 exactly.' \
            'Set PYTHON to a Python 3.12 executable before creating the environment.' >&2
        exit 2
    fi
    "$PYTHON" -m venv "$VENV"
    ENV_PYTHON="$VENV/bin/python"
fi

if ! "$ENV_PYTHON" -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))'; then
    printf '%s\n' \
        "The selected environment is not Python 3.12: $ENV_PYTHON" \
        'Choose another VENV path or remove/recreate that local environment with Python 3.12.' >&2
    exit 2
fi

"$ENV_PYTHON" -m pip install --require-hashes -r "$ROOT_DIR/worker/requirements-dev.lock"
"$ENV_PYTHON" -m pip install --no-deps --no-build-isolation -e "$ROOT_DIR/worker"

printf 'DocGenerator development environment ready: %s\n' "$ENV_PYTHON"
