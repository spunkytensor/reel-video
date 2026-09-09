#!/bin/sh
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -eu

python ci/check_headers.py

# Explicit real CPU contracts. No patched inference, fake workers, or GPU skips.
python -m ruff check h3.py image_codec.py provider.py server.py worker.py ci/check_sbom.py ci/check_headers.py ci/check_cve_baseline.py ci/test_runtime_media.py
python -m ruff format --check h3.py image_codec.py provider.py server.py worker.py ci/check_sbom.py ci/check_headers.py ci/check_cve_baseline.py ci/test_runtime_media.py
python -m pytest -q --junitxml=outputs/ci/python.xml \
  test_h3.py::test_frame_count \
  test_h3.py::test_invalid_duration \
  test_h3.py::test_valid_options \
  test_h3.py::test_invalid_options \
  test_h3.py::test_preserves_existing_output \
  test_h3.py::test_generation_workflow_selects_image_mode \
  test_h3.py::test_generation_workflow_rejects_references_mixed_with_keyframes \
  test_h3.py::test_generation_workflow_rejects_more_than_nine_references \
  test_server.py::test_capabilities_exact_contract \
  test_server.py::test_rejects_unsupported_fields_even_when_they_match \
  test_server.py::test_strict_request_combinations \
  test_server.py::test_durable_queue_idempotency_conflict_and_overload \
  test_server.py::test_bearer_cookie_csrf_host_and_logout \
  test_server.py::test_delete_pending_is_durable_hidden_and_idempotent \
  test_server.py::test_studio_automatic_session_keeps_api_key_private \
  test_server.py::test_environment_configuration_in_fresh_process \
  test_images.py::test_raw_upload_auth_contract_and_normalized_get_head \
  test_images.py::test_session_upload_requires_csrf_origin \
  test_images.py::test_fake_and_streamed_oversize_uploads_are_rejected \
  test_images.py::test_roles_order_canonical_urls_and_strict_model_parameters \
  test_images.py::test_general_reference_aspect_and_materialization_order \
  test_images.py::test_reference_conditioning_budget_rejects_oom_profile_before_enqueue \
  test_image_codec.py::test_decodes_supported_formats_to_canonical_rgb_png \
  test_image_codec.py::test_strips_png_text_and_color_metadata \
  test_image_codec.py::test_applies_exif_orientation \
  test_image_codec.py::test_composites_transparency_onto_white \
  test_image_codec.py::test_rejects_empty_fake_and_truncated_data \
  test_image_codec.py::test_rejects_wrong_mime_and_unsupported_mime \
  test_image_codec.py::test_rejects_animated_webp_and_png \
  test_image_codec.py::test_rejects_animated_gif_as_unsupported \
  test_worker.py::test_cpu_ffmpeg_finalize_contract \
  test_worker.py::test_invalid_source_is_never_published
