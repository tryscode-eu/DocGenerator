# Migration provenance — document worker

## Decision

The local `WKR_document_generator` directory is the RabbitMQ worker of the
existing DocGenerator product. It is therefore integrated into this repository
under `worker/` instead of becoming a separate repository.

## Source and destination

- Migration date: 2026-07-14.
- Source: sibling local directory `WKR_document_generator`.
- Source Git status: no `.git` directory; no source commit, tag or author
  history was available to preserve.
- Destination repository: `git@github.com:tryscode-eu/DocGenerator.git`.
- Destination base: `main` at `65a360e`.
- Migration branch: `agent/integrate-document-worker-20260714`.
- Destination: `worker/`, with repository-wide CI under `.github/workflows/`.

Before migration, 21 non-cache source files produced the ordered manifest
SHA-256:

```text
66ea7f2004cc8eb30f9c668a2b15cfdb92f9863290d1cf0faa4d8131d25ae5f0
```

The exact 21 path/file hashes used to derive that aggregate are versioned in
`SOURCE_MANIFEST.sha256`; generated caches and `.DS_Store` are excluded.

The source test suite passed 124 tests on Python 3.12.13 before any destination
adaptation. High-confidence secret signatures found in the source tree: zero.

## Mapping

- `doc_worker/` → `worker/doc_worker/`.
- `tests/` → `worker/tests/`.
- `scripts/` → `worker/scripts/`.
- `.env.example`, `.dockerignore`, `.gitignore`, `Dockerfile`, `README.md` →
  corresponding files under `worker/`.
- Source `requirements.txt` → `worker/pyproject.toml`,
  `worker/requirements.lock` and `worker/requirements-dev.lock`.
- Source `.github/workflows/ci.yml` → repository-wide
  `.github/workflows/ci.yml`, adapted to the new paths and extended to cover
  both the ODT engine and worker packaging/image.

The following generated local state was deliberately excluded:

- `.pytest_cache/`;
- `.ruff_cache/`;
- every `__pycache__/` directory and `*.pyc` file;
- `.DS_Store` files.

## Destination adaptations

The destination intentionally differs from a byte-for-byte copy where product
integration or security required it:

- repository-wide SHA-pinned CI, a high-confidence secret scanner, exact
  Python 3.12 enforcement and hash-locked runtime/development environments;
- package metadata and an image context that includes the worker README while
  excluding local state and secrets;
- mandatory, redirect-resistant Harmony success/failure callbacks and removal
  of S3 staging bytes before the terminal success callback;
- HMAC-authenticated retry state and compact callback-only continuations, so a
  callback outage neither republishes learner document data nor reruns a
  completed renderer;
- ODT input through a private regular file or stdin instead of process `argv`,
  strict bounded JSON, XML escaping and portable `libreoffice`/`soffice`
  discovery;
- evidence URLs without credentials, query or fragment, so a rendered PDF
  cannot preserve a presigned secret;
- formatting and tests for the migrated paths, packaging, ODT engine, storage,
  HTTP boundary and document contracts.

## History and cutover limits

Because the source was not a Git repository, its pre-migration commit history
cannot be reconstructed. The manifest hash above preserves snapshot provenance,
while this destination branch provides the first reviewable Git history.

The isolated source directory must remain unchanged until this branch is
reviewed, its CI is green, it is merged into `main`, and `main` is verified.
Only then may the old directory be archived or removed through a separate,
explicitly validated cleanup. During review, only the copy under `worker/`
should receive changes; the two locations must not become independently
maintained active workers.
