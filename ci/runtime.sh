#!/bin/sh
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -eu
mkdir -p outputs/ci
# No GPU flags, weights, production state, credentials or network access.
docker run --rm --network none --entrypoint python \
  -e PYTHONPATH=/app \
  -v "$PWD/test_worker.py:/tests/test_worker.py:ro" \
  -v "$PWD/ci/test_runtime_media.py:/tests/ci/test_runtime_media.py:ro" \
  -v "$PWD/outputs/ci:/evidence" \
  "${1:-reel-video:ci}" -m pytest -q -p no:cacheprovider --tb=short \
  --junitxml=/evidence/runtime.xml \
  /tests/ci/test_runtime_media.py \
  /tests/test_worker.py::test_cpu_ffmpeg_finalize_contract \
  /tests/test_worker.py::test_invalid_source_is_never_published
