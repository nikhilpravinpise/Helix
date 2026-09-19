#!/usr/bin/env bash
# Bring up the Kafka backbone and declare its topics (subphase E2).
# Waits for the broker healthcheck, then creates topics idempotently, so
# callers can produce immediately. Safe to re-run.
#
# Topics are created here rather than left to auto-creation because
# partition count and retention are part of the design: auto-created
# topics get the broker default (1 partition, global retention), which
# would silently drop the per-incident ordering the partition key exists
# to provide.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${REPO_ROOT}/infra/kafka-compose.yml"
CONTAINER="helix-kafka"
BOOTSTRAP="localhost:9092"

# 3 partitions: enough for a consumer group to scale past one member while
# keeping every event for one correlation_id on a single partition, which
# is what preserves per-incident ordering. Retention 7 days.
EVENTS_TOPIC="helix.events"
EVENTS_PARTITIONS=3
EVENTS_RETENTION_MS=604800000

# 1 partition: ordering across poison messages does not matter and a
# single partition keeps them trivially readable. Retention 30 days,
# deliberately longer than the main topic - a dead letter is evidence, and
# it is worth keeping past the point where the original event has aged
# out.
DLQ_TOPIC="helix.events.dlq"
DLQ_PARTITIONS=1
DLQ_RETENTION_MS=2592000000

docker compose -f "${COMPOSE_FILE}" up -d --wait

create_topic() {
  local name="$1" partitions="$2" retention_ms="$3"
  docker exec "${CONTAINER}" /opt/kafka/bin/kafka-topics.sh \
    --bootstrap-server "${BOOTSTRAP}" \
    --create --if-not-exists \
    --topic "${name}" \
    --partitions "${partitions}" \
    --replication-factor 1 \
    --config "retention.ms=${retention_ms}" >/dev/null
  echo "topic ready: ${name} (partitions=${partitions}, retention.ms=${retention_ms})"
}

create_topic "${EVENTS_TOPIC}" "${EVENTS_PARTITIONS}" "${EVENTS_RETENTION_MS}"
create_topic "${DLQ_TOPIC}" "${DLQ_PARTITIONS}" "${DLQ_RETENTION_MS}"

echo "kafka up: 127.0.0.1:9092"
