#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORKER_DIR=$(dirname -- "$SCRIPT_DIR")
HARMONY_DIR=${HARMONY_DIR:-"$WORKER_DIR/../../FW_Harmony"}
MINIO_IMAGE=${MINIO_IMAGE:-"minio/minio:RELEASE.2025-09-07T16-13-09Z"}
PROOF_IMAGE=${PROOF_IMAGE:-"tryscode/harmony:artifact-dev"}

if [ ! -d "$HARMONY_DIR/app" ]; then
    echo "Harmony source directory not found: $HARMONY_DIR" >&2
    exit 2
fi

suffix="$(date -u +%Y%m%d%H%M%S)-$$-${RANDOM:-0}"
container="tryscode-minio-proof-$suffix"
network="$container-net"
volume="$container-data"
bucket="tryscode-artifact-proof-$suffix"
prefix="proof-$suffix/private"
access_key="minio-proof-access"
secret_key="minio-proof-secret-not-for-production"
network_created=0
volume_created=0
container_created=0

cleanup() {
    set +e
    if [ "$container_created" -eq 1 ]; then
        docker rm -f "$container" >/dev/null 2>&1
        container_created=0
    fi
    if [ "$volume_created" -eq 1 ]; then
        docker volume rm "$volume" >/dev/null 2>&1
        volume_created=0
    fi
    if [ "$network_created" -eq 1 ]; then
        docker network rm "$network" >/dev/null 2>&1
        network_created=0
    fi
    set -e
}
trap cleanup EXIT INT TERM

docker network create "$network" >/dev/null
network_created=1
docker volume create "$volume" >/dev/null
volume_created=1
docker run -d \
    --name "$container" \
    --network "$network" \
    --network-alias minio-proof \
    -e MINIO_ROOT_USER="$access_key" \
    -e MINIO_ROOT_PASSWORD="$secret_key" \
    -v "$volume:/data" \
    "$MINIO_IMAGE" \
    server /data --address :9000 >/dev/null
container_created=1

docker exec "$container" minio --version
docker image inspect "$MINIO_IMAGE" --format 'minio_image={{.RepoTags}} image_id={{.Id}}'
docker image inspect "$PROOF_IMAGE" --format 'proof_image={{.RepoTags}} image_id={{.Id}}'

set +e
docker run --rm \
    --network "$network" \
    -e AWS_EC2_METADATA_DISABLED=true \
    -e RABBITMQ_URL=amqp://guest:guest@127.0.0.1:5672/ \
    -e ELASTIC_HOST=http://127.0.0.1:9200 \
    -e DB_SYNC_URI=postgresql://proof:proof@127.0.0.1/proof \
    -e DB_ASYNC_URI=postgresql+asyncpg://proof:proof@127.0.0.1/proof \
    -e DEBUG=true \
    -e STORAGE_MODE=s3 \
    -e S3_ENDPOINT_URL=http://minio-proof:9000 \
    -e S3_BUCKET="$bucket" \
    -e S3_ACCESS_KEY="$access_key" \
    -e S3_SECRET_KEY="$secret_key" \
    -e S3_REGION=us-east-1 \
    -e S3_PREFIX="$prefix" \
    -e S3_DIR_DOCUMENT=documents \
    -e PYTHONPATH=/workspace/worker:/workspace/harmony \
    -v "$WORKER_DIR:/workspace/worker:ro" \
    -v "$HARMONY_DIR:/workspace/harmony:ro" \
    -w /tmp \
    "$PROOF_IMAGE" \
    python /workspace/worker/scripts/prove_minio_artifact_chain.py
proof_status=$?
set -e

cleanup
trap - EXIT INT TERM

cleanup_status=0
if docker inspect "$container" >/dev/null 2>&1; then
    echo "temporary MinIO container still exists" >&2
    cleanup_status=1
fi
if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "temporary MinIO volume still exists" >&2
    cleanup_status=1
fi
if docker network inspect "$network" >/dev/null 2>&1; then
    echo "temporary MinIO network still exists" >&2
    cleanup_status=1
fi
if [ "$cleanup_status" -eq 0 ]; then
    echo '{"cleanup":{"container_deleted":true,"network_deleted":true,"volume_deleted":true}}'
fi

if [ "$proof_status" -ne 0 ]; then
    exit "$proof_status"
fi
exit "$cleanup_status"
