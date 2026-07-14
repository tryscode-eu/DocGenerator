#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
WORKER_DIR=$(dirname -- "$SCRIPT_DIR")
RABBITMQ_IMAGE=${RABBITMQ_IMAGE:-"rabbitmq:3.13-management-alpine@sha256:606d8c0d6b3c18d1da9afc53bc7cdb2a8d5486df91b5a9830e9e07626c9ae281"}
EXPECTED_RABBITMQ_VERSION=${EXPECTED_RABBITMQ_VERSION:-"3.13.7"}

case "$RABBITMQ_IMAGE" in
    rabbitmq:3.13-management-alpine@sha256:*) ;;
    *)
        echo "RABBITMQ_IMAGE must be an immutable RabbitMQ 3.13 management-alpine digest" >&2
        exit 2
        ;;
esac

if ! printf '%s\n' "$EXPECTED_RABBITMQ_VERSION" | grep -Eq '^3\.13\.[0-9]+$'; then
    echo "EXPECTED_RABBITMQ_VERSION must be an explicit RabbitMQ 3.13 patch" >&2
    exit 2
fi

suffix="$(date -u +%Y%m%d%H%M%S)-$$"
rabbit_container="tryscode-rabbitmq-proof-$suffix"
proof_container="tryscode-document-archive-proof-$suffix"
network="$rabbit_container-net"
volume="$rabbit_container-data"
proof_image="tryscode/docgenerator-worker:rabbitmq-archive-proof-$suffix"
broker_username="tryscode-proof-worker"
broker_password="tryscode-proof-password-not-production"
erlang_cookie="tryscode-proof-cookie-not-production"
signing_key="tryscode-proof-document-retry-signing-key-not-production"
callback_token="tryscode-proof-document-callback-token-not-production"
replay_confirmation="REPLAY_DISPOSABLE_DOCUMENT_ARCHIVE_PROOF"

network_created=0
volume_created=0
rabbit_created=0
proof_image_created=0

cleanup() {
    set +e
    docker rm -f "$proof_container" >/dev/null 2>&1
    if [ "$rabbit_created" -eq 1 ]; then
        docker rm -f "$rabbit_container" >/dev/null 2>&1
        rabbit_created=0
    fi
    if [ "$network_created" -eq 1 ]; then
        docker network rm "$network" >/dev/null 2>&1
        network_created=0
    fi
    if [ "$volume_created" -eq 1 ]; then
        docker volume rm "$volume" >/dev/null 2>&1
        volume_created=0
    fi
    if [ "$proof_image_created" -eq 1 ]; then
        docker image rm "$proof_image" >/dev/null 2>&1
        proof_image_created=0
    fi
    set -e
}
trap cleanup EXIT INT TERM

if ! docker image inspect "$RABBITMQ_IMAGE" >/dev/null 2>&1; then
    docker pull "$RABBITMQ_IMAGE"
fi
docker build --pull=false --tag "$proof_image" "$WORKER_DIR"
proof_image_created=1

docker network create --internal "$network" >/dev/null
network_created=1
docker volume create "$volume" >/dev/null
volume_created=1
docker run --detach \
    --name "$rabbit_container" \
    --network "$network" \
    --network-alias rabbitmq-proof \
    --env RABBITMQ_DEFAULT_USER="$broker_username" \
    --env RABBITMQ_DEFAULT_PASS="$broker_password" \
    --env RABBITMQ_DEFAULT_VHOST=/ \
    --env RABBITMQ_ERLANG_COOKIE="$erlang_cookie" \
    --volume "$volume:/var/lib/rabbitmq" \
    "$RABBITMQ_IMAGE" >/dev/null
rabbit_created=1

ready=0
attempt=0
while [ "$attempt" -lt 60 ]; do
    if docker exec --user rabbitmq "$rabbit_container" \
        rabbitmq-diagnostics -q check_running >/dev/null 2>&1; then
        ready=1
        break
    fi
    running=$(docker inspect "$rabbit_container" --format '{{.State.Running}}')
    if [ "$running" != "true" ]; then
        echo "Disposable RabbitMQ exited during startup" >&2
        docker logs --tail 100 "$rabbit_container" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep 1
done
if [ "$ready" -ne 1 ]; then
    echo "Disposable RabbitMQ did not become ready" >&2
    exit 1
fi

actual_version=$(docker exec --user rabbitmq "$rabbit_container" \
    rabbitmqctl version | tr -d '\r')
if [ "$actual_version" != "$EXPECTED_RABBITMQ_VERSION" ]; then
    echo "Disposable RabbitMQ version differs from the explicit proof pin" >&2
    exit 1
fi

feature_flags=$(docker exec --user rabbitmq "$rabbit_container" \
    rabbitmqctl -q list_feature_flags name state)
if ! printf '%s\n' "$feature_flags" | grep -Eq '^stream_queue[[:space:]]+enabled$'; then
    echo "RabbitMQ stream_queue feature flag is not enabled" >&2
    exit 1
fi

docker image inspect "$RABBITMQ_IMAGE" \
    --format 'rabbitmq_image_id={{.Id}} rabbitmq_repo_digests={{json .RepoDigests}}'
docker image inspect "$proof_image" \
    --format 'worker_proof_image_id={{.Id}}'

set +e
docker run --rm \
    --name "$proof_container" \
    --network "$network" \
    --read-only \
    --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
    --user 10001:10001 \
    --workdir /tmp \
    --env RABBITMQ_URL="amqp://$broker_username:$broker_password@rabbitmq-proof:5672/%2F" \
    --env DOCUMENT_RETRY_SIGNING_KEY="$signing_key" \
    --env HARMONY_SERVICE_TOKEN="$callback_token" \
    --env DOCUMENT_ARCHIVE_PROOF_CONFIRMATION="$replay_confirmation" \
    --env RABBITMQ_EXPECTED_VERSION="$EXPECTED_RABBITMQ_VERSION" \
    --env PYTHONPATH=/workspace/worker \
    --mount "type=bind,source=$WORKER_DIR,target=/workspace/worker,readonly" \
    "$proof_image" \
    python /workspace/worker/scripts/prove_rabbitmq_archive.py
proof_status=$?
set -e

if [ "$proof_status" -eq 0 ]; then
    docker exec --user rabbitmq "$rabbit_container" rabbitmqctl -q list_queues -p / \
        name type durable messages_ready messages_unacknowledged arguments
fi

cleanup
trap - EXIT INT TERM

cleanup_status=0
if docker inspect "$proof_container" >/dev/null 2>&1; then
    echo "Temporary document proof container still exists" >&2
    cleanup_status=1
fi
if docker inspect "$rabbit_container" >/dev/null 2>&1; then
    echo "Temporary RabbitMQ container still exists" >&2
    cleanup_status=1
fi
if docker network inspect "$network" >/dev/null 2>&1; then
    echo "Temporary RabbitMQ proof network still exists" >&2
    cleanup_status=1
fi
if docker volume inspect "$volume" >/dev/null 2>&1; then
    echo "Temporary RabbitMQ proof volume still exists" >&2
    cleanup_status=1
fi
if docker image inspect "$proof_image" >/dev/null 2>&1; then
    echo "Temporary document proof image still exists" >&2
    cleanup_status=1
fi
if [ "$cleanup_status" -eq 0 ]; then
    echo '{"cleanup":{"network_deleted":true,"proof_container_deleted":true,"rabbitmq_container_deleted":true,"rabbitmq_volume_deleted":true,"worker_proof_image_deleted":true}}'
fi

if [ "$proof_status" -ne 0 ]; then
    exit "$proof_status"
fi
exit "$cleanup_status"
