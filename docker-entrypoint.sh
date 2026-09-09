#!/bin/sh
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -eu

model_path=$(python -c 'import h3; print(h3.MODEL_PATH)')
model_marker="$model_path/.docker-download-complete"
mkdir -p /app/models /app/state
umask 077
: "${REEL_VIDEO_API_TOKEN:?Set REEL_VIDEO_API_TOKEN in .env and start with run.sh}"
export REEL_VIDEO_API_TOKEN

container_ip=$(hostname -i | awk '{print $1}')
if [ -z "$container_ip" ]; then
    echo "Could not determine the container address." >&2
    exit 1
fi

public_port=${REEL_VIDEO_PUBLIC_PORT:-8088}
network_hosts=${REEL_VIDEO_NETWORK_HOSTS:?REEL_VIDEO_NETWORK_HOSTS must be set by run.sh}
export REEL_VIDEO_BASE_URL="http://127.0.0.1:$public_port"
REEL_VIDEO_ALLOWED_ORIGINS=''
old_ifs=$IFS
IFS=,
for network_host in $network_hosts; do
    origin="http://$network_host:$public_port"
    if [ -n "$REEL_VIDEO_ALLOWED_ORIGINS" ]; then
        REEL_VIDEO_ALLOWED_ORIGINS="$REEL_VIDEO_ALLOWED_ORIGINS,$origin"
    else
        REEL_VIDEO_ALLOWED_ORIGINS=$origin
    fi
done
IFS=$old_ifs
export REEL_VIDEO_ALLOWED_ORIGINS
export REEL_VIDEO_BIND="$container_ip"
export REEL_VIDEO_PORT=8088
export REEL_VIDEO_STATE_DIR=/app/state

if [ -e "$model_marker" ]; then
    model_status='cached'
else
    model_status='downloading now; follow the container logs for progress'
fi

printf '\nReel Video is starting in the background.\n'
printf 'Studio (this computer): http://127.0.0.1:%s\n' "$public_port"
IFS=,
for network_host in $network_hosts; do
    printf 'Studio (private network): http://%s:%s\n' "$network_host" "$public_port"
done
IFS=$old_ifs
printf 'Model weights: %s\n' "$model_status"
printf '\n'

if [ ! -e "$model_marker" ]; then
    python /app/h3.py download --references
    touch "$model_marker"
fi

exec python /app/server.py
