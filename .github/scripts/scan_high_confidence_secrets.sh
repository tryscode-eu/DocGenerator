#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
PYTHON=${PYTHON:-python3}

exec "$PYTHON" - "$ROOT_DIR" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1]).resolve()
excluded = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", "build", "dist"}
patterns = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{36,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{50,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(rb"\bsk_(?:live|prod)_[A-Za-z0-9]{20,}\b"),
    re.compile(rb"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
)

matches = []
for path in root.rglob("*"):
    if not path.is_file() or excluded.intersection(path.relative_to(root).parts):
        continue
    try:
        content = path.read_bytes()
    except OSError:
        continue
    if b"\x00" in content[:8192]:
        continue
    if any(pattern.search(content) for pattern in patterns):
        matches.append(path.relative_to(root).as_posix())

if matches:
    print("High-confidence secret signatures detected in:", file=sys.stderr)
    for match in sorted(matches):
        print(f"- {match}", file=sys.stderr)
    raise SystemExit(1)

print("No high-confidence secret signature detected.")
PY
