#!/usr/bin/env bash
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
# Match run.sh's configuration, but allow stopping without a key or .env.
if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi
CONTAINER="${REEL_VIDEO_CONTAINER_NAME:-reel-video}"

command -v docker >/dev/null || {
  echo "Docker is required: https://docs.docker.com/engine/install/" >&2
  exit 1
}

if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or is not accessible by this user." >&2
  exit 1
fi

# Listing distinguishes an absent container from a Docker access failure.
containers="$(docker container ls --all --format '{{.Names}}')"
while IFS= read -r name; do
  if [[ "$name" == "$CONTAINER" ]]; then
    docker stop --timeout 25 -- "$CONTAINER" >/dev/null
    printf 'Stopped %s. Model and state volumes are retained.\n' "$CONTAINER"
    exit 0
  fi
done <<<"$containers"

printf '%s is already stopped (no container found).\n' "$CONTAINER"
