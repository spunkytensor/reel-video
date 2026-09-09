#!/usr/bin/env bash
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
ENV_FILE="$ROOT/.env"
if [[ ! -f "$ENV_FILE" ]]; then
  echo "Create $ENV_FILE from .env.example and set REEL_VIDEO_API_TOKEN before starting." >&2
  exit 1
fi
# .env is trusted local shell configuration. Export values for Docker's environment.
unset REEL_VIDEO_API_TOKEN
set -a
source "$ENV_FILE"
set +a
valid_api_token() {
  local LC_ALL=C
  [[ "${REEL_VIDEO_API_TOKEN:-}" =~ ^[[:graph:]]{32,}$ && "$REEL_VIDEO_API_TOKEN" != replace-* ]]
}
if ! valid_api_token; then
  echo "Set REEL_VIDEO_API_TOKEN in .env to a unique ASCII key of at least 32 characters." >&2
  exit 1
fi
export REEL_VIDEO_API_TOKEN

IMAGE="${REEL_VIDEO_DOCKER_IMAGE:-reel-video:local}"
PORT="${REEL_VIDEO_PORT:-8088}"
MODELS_VOLUME="${REEL_VIDEO_MODELS_VOLUME:-reel-video-models}"
STATE_VOLUME="${REEL_VIDEO_STATE_VOLUME:-reel-video-state}"
CONTAINER="${REEL_VIDEO_CONTAINER_NAME:-reel-video}"

command -v docker >/dev/null || {
  echo "Docker is required: https://docs.docker.com/engine/install/" >&2
  exit 1
}

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or is not accessible by this user." >&2
  exit 1
fi

network_hosts=()
add_network_host() {
  local host="$1" existing
  host="${host#"${host%%[![:space:]]*}"}"
  host="${host%"${host##*[![:space:]]}"}"
  [[ -n "$host" ]] || return
  if [[ "$host" == *:* || "$host" == */* ]]; then
    echo "Network hosts must not include a scheme or port: $host" >&2
    exit 1
  fi
  for existing in "${network_hosts[@]}"; do
    [[ "$existing" != "$host" ]] || return 0
  done
  network_hosts+=("$host")
}

configured_hosts="${REEL_VIDEO_NETWORK_HOSTS:-}"
if [[ -n "$configured_hosts" ]]; then
  IFS=',' read -ra candidates <<<"$configured_hosts"
  for candidate in "${candidates[@]}"; do
    add_network_host "$candidate"
  done
else
  primary_ip=""
  if command -v ip >/dev/null; then
    primary_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')"
  fi
  if [[ -z "$primary_ip" ]]; then
    primary_ip="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i !~ /^127\./ && $i !~ /:/) {print $i; exit}}')"
  fi
  add_network_host "$primary_ip"
  fqdn="$(hostname -f 2>/dev/null || true)"
  if [[ "$fqdn" == *.* && "$fqdn" != localhost.* ]]; then
    add_network_host "$fqdn"
  fi
fi
if (( ${#network_hosts[@]} == 0 )); then
  echo "No private network host was found. Set REEL_VIDEO_NETWORK_HOSTS to an IP address or hostname." >&2
  exit 1
fi
network_hosts_csv="$(IFS=,; echo "${network_hosts[*]}")"
if [[ ! "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "REEL_VIDEO_PORT must be a number from 1 through 65535." >&2
  exit 1
fi

docker build --tag "$IMAGE" "$ROOT"

# The worker must create user/network namespaces before inference. Docker's
# default seccomp profile blocks this; unconfined permits it but weakens the
# container boundary. Use only on a trusted host, not for hostile tenants.
docker run --detach --rm --init \
  --name "$CONTAINER" \
  --gpus all \
  --shm-size 16g \
  --stop-timeout 25 \
  --security-opt seccomp=unconfined \
  --publish "$PORT:8088" \
  --volume "$MODELS_VOLUME:/app/models" \
  --volume "$STATE_VOLUME:/app/state" \
  --env "REEL_VIDEO_NETWORK_HOSTS=$network_hosts_csv" \
  --env "REEL_VIDEO_PUBLIC_PORT=$PORT" \
  --env REEL_VIDEO_API_TOKEN \
  --env "REEL_VIDEO_RETENTION_DAYS=${REEL_VIDEO_RETENTION_DAYS:-3}" \
  --env "REEL_VIDEO_QUOTA_GIB=${REEL_VIDEO_QUOTA_GIB:-25}" \
  ${HF_TOKEN:+--env HF_TOKEN} \
  "$IMAGE" >/dev/null

for _ in {1..50}; do
  logs="$(docker logs "$CONTAINER" 2>&1 || true)"
  if grep -q '^Model weights:' <<<"$logs"; then
    awk '/^Reel Video is starting/{printing=1} printing{print} /^Model weights:/{exit}' <<<"$logs"
    printf '\nContainer: %s\n' "$CONTAINER"
    printf 'Follow progress: docker logs --follow %s\n' "$CONTAINER"
    printf 'Stop: REEL_VIDEO_CONTAINER_NAME=%q %q\n' "$CONTAINER" "$ROOT/stop.sh"
    exit 0
  fi
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" != true ]]; then
    printf '%s\n' "$logs" >&2
    echo "The Reel Video container stopped before startup completed." >&2
    exit 1
  fi
  sleep 0.1
done

echo "The container started, but its startup information was not available." >&2
echo "Inspect it with: docker logs $CONTAINER" >&2
exit 1
