PYTHON ?= python3
VENV ?= .venv
VENV_PYTHON := $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV)/bin/python,$(abspath $(VENV))/bin/python)
DIST_DIR ?= /tmp/tryscode-docgenerator-dist
ODT_TEMPLATE ?= templates/test_template.odt
ODT_OUTPUT ?= test_template.pdf
DATA_FILE ?=
PYTHON_LINT_PATHS := main.py tests worker/doc_worker worker/tests
PROOF_PYTHON_SCRIPTS := \
	worker/scripts/prove_minio_artifact_chain.py \
	worker/scripts/prove_rabbitmq_archive.py
PROOF_SHELL_SCRIPTS := \
	worker/scripts/run_minio_artifact_proof.sh \
	worker/scripts/run_rabbitmq_archive_proof.sh

.PHONY: install lint check-scripts test scan-secrets build-package docker-worker run-worker run-odt

install:
	PYTHON="$(PYTHON)" VENV="$(VENV)" sh install.sh

lint: check-scripts
	$(VENV_PYTHON) -m ruff check $(PYTHON_LINT_PATHS) $(PROOF_PYTHON_SCRIPTS)
	$(VENV_PYTHON) -m ruff format --check $(PYTHON_LINT_PATHS) $(PROOF_PYTHON_SCRIPTS)

check-scripts:
	$(VENV_PYTHON) -m py_compile $(PROOF_PYTHON_SCRIPTS)
	@for script in $(PROOF_SHELL_SCRIPTS); do sh -n "$$script"; done

test:
	PYTHONDONTWRITEBYTECODE=1 $(VENV_PYTHON) -m pytest -q -p no:cacheprovider tests worker/tests

scan-secrets:
	sh .github/scripts/scan_high_confidence_secrets.sh

build-package:
	mkdir -p "$(DIST_DIR)"
	$(VENV_PYTHON) -m build --no-isolation --wheel --outdir "$(DIST_DIR)" worker

docker-worker:
	docker build --file worker/Dockerfile --tag tryscode/docgenerator-worker:dev worker

run-worker:
	cd worker && "$(VENV_PYTHON)" -m doc_worker.main

run-odt:
	@test -n "$(DATA_FILE)" || { echo 'DATA_FILE is required (use a mode-0600 JSON file)' >&2; exit 2; }
	$(VENV_PYTHON) main.py "$(ODT_TEMPLATE)" "$(ODT_OUTPUT)" --data-file "$(DATA_FILE)"
