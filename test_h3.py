# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

import json
import os
from argparse import Namespace
from pathlib import Path

import pytest
from PIL import Image

import h3
from h3 import frame_count, validate_generation


@pytest.mark.parametrize("seconds,expected", [(5, 124), (10, 243), (14.375, 345)])
def test_frame_count(seconds, expected):
    assert frame_count(seconds) == expected


@pytest.mark.parametrize("seconds", [0, 4.9, 15, 16, float("nan"), float("inf")])
def test_invalid_duration(seconds):
    with pytest.raises(ValueError):
        frame_count(seconds)


def options(tmp_path, **changes):
    values = {
        "prompt": "A fox in the snow",
        "seconds": 5,
        "width": 960,
        "height": 544,
        "steps": 50,
        "seed": 42,
        "output": tmp_path / "video.mp4",
    }
    return Namespace(**(values | changes))


def test_valid_options(tmp_path):
    validate_generation(options(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt": " "},
        {"width": 961},
        {"height": 0},
        {"steps": 1},
        {"seed": -1},
        {"width": 2048, "height": 2048},
        {"width": 32, "height": 768},
        {"output": Path("out.wav")},
    ],
)
def test_invalid_options(tmp_path, changes):
    with pytest.raises(ValueError):
        validate_generation(options(tmp_path, **changes))


@pytest.mark.parametrize("suffix", [".mp4", ".json"])
def test_preserves_existing_output(tmp_path, suffix):
    (tmp_path / f"video{suffix}").write_text("existing")
    with pytest.raises(ValueError, match="already exists"):
        validate_generation(options(tmp_path))


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "t2va"),
        ({"first_frame": Path("first.png")}, "fl2va"),
        ({"last_frame": Path("last.png")}, "fl2va"),
        ({"reference_image": [Path("reference.png")]}, "ref2va"),
    ],
)
def test_generation_workflow_selects_image_mode(tmp_path, changes, expected):
    assert h3.generation_workflow(options(tmp_path, **changes)) == expected


@pytest.mark.parametrize("frame_name", ["first_frame", "last_frame"])
def test_generation_workflow_rejects_references_mixed_with_keyframes(
    tmp_path, frame_name
):
    args = options(
        tmp_path,
        reference_image=[Path("reference.png")],
        **{frame_name: Path("frame.png")},
    )
    with pytest.raises(ValueError, match="OR general references"):
        h3.generation_workflow(args)


def test_generation_workflow_rejects_more_than_nine_references(tmp_path):
    args = options(tmp_path, reference_image=[Path(f"{i}.png") for i in range(10)])
    with pytest.raises(ValueError, match="at most nine"):
        h3.generation_workflow(args)


def save_image(path, mode, color, size=(64, 32)):
    Image.new(mode, size, color).save(path)


def test_image_inputs_decodes_rgb_and_preserves_roles_and_reference_order(tmp_path):
    first = tmp_path / "first.png"
    last = tmp_path / "last.png"
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    save_image(first, "RGBA", (1, 2, 3, 128))
    save_image(last, "L", 12)
    save_image(red, "RGB", (255, 0, 0))
    save_image(blue, "RGB", (0, 0, 255))

    keyframes = h3.image_inputs(options(tmp_path, first_frame=first, last_frame=last))
    assert list(keyframes) == ["image", "last_image"]
    assert keyframes["image"].mode == keyframes["last_image"].mode == "RGB"
    assert keyframes["image"].getpixel((0, 0)) == (1, 2, 3)

    references = h3.image_inputs(options(tmp_path, reference_image=[red, blue]))
    assert [
        reference.image.getpixel((0, 0)) for reference in references["references"]
    ] == [
        (255, 0, 0),
        (0, 0, 255),
    ]
    assert all(reference.image.mode == "RGB" for reference in references["references"])


def test_image_inputs_rejects_animation(tmp_path):
    animated = tmp_path / "animated.gif"
    frames = [Image.new("RGB", (32, 32), color) for color in ("red", "blue")]
    frames[0].save(animated, save_all=True, append_images=frames[1:])
    with pytest.raises(ValueError, match="still images"):
        h3.image_inputs(options(tmp_path, first_frame=animated))


@pytest.mark.parametrize("size", [(128, 16), (16, 128)])
def test_image_inputs_rejects_invalid_aspect_ratio(tmp_path, size):
    image = tmp_path / "wide.png"
    save_image(image, "RGB", "red", size=size)
    with pytest.raises(ValueError, match="aspect ratio"):
        h3.image_inputs(options(tmp_path, first_frame=image))


@pytest.mark.parametrize("field", ["first_frame", "last_frame", "reference_image"])
def test_image_inputs_never_downloads_urls(tmp_path, monkeypatch, field):
    import urllib.request

    import requests

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *args, **kwargs: pytest.fail("image_inputs attempted a URL download"),
    )
    monkeypatch.setattr(
        requests.Session,
        "request",
        lambda *args, **kwargs: pytest.fail("image_inputs attempted an HTTP request"),
    )
    url = "https://example.com/image.png"
    value = [url] if field == "reference_image" else url
    with pytest.raises(FileNotFoundError):
        h3.image_inputs(options(tmp_path, **{field: value}))


@pytest.mark.parametrize("include_references", [False, True])
def test_download_uses_pinned_revision_and_component_patterns(
    monkeypatch, include_references
):
    import huggingface_hub

    calls = []
    monkeypatch.setattr(
        huggingface_hub,
        "snapshot_download",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    h3.download(include_references=include_references)

    assert len(calls) == 1
    positional, keywords = calls[0]
    assert positional == (h3.MODEL_ID,)
    assert keywords["revision"] == h3.MODEL_REVISION
    assert keywords["local_dir"] == h3.MODEL_PATH
    expected_components = (
        (*h3.COMPONENTS, "transformer_ref") if include_references else h3.COMPONENTS
    )
    assert keywords["allow_patterns"] == [
        "LICENSE",
        "README.md",
        "model_index.json",
        "modular_model_index.json",
        *[f"{name}/*" for name in expected_components],
    ]
    assert keywords["max_workers"] == 4


def test_generation_contract(tmp_path, monkeypatch):
    """Exercise the wrapper without model weights, including the TorchAO mode fix."""
    import torch
    from diffusers.utils import export_utils

    args = options(tmp_path)
    video, audio = object(), object()

    def pipeline(**kwargs):
        assert not torch.is_grad_enabled()
        assert not torch.is_inference_mode_enabled()
        assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["TRANSFORMERS_OFFLINE"] == "1"
        assert kwargs["num_frames"] == 124
        assert kwargs["num_inference_steps"] == 50
        assert kwargs["generator"].initial_seed() == 42
        assert kwargs["output"] == ["videos", "audio", "sampling_rate"]
        return {"videos": [video], "audio": [audio], "sampling_rate": 32000}

    def encode(frames, **kwargs):
        assert frames is video
        assert kwargs["audio"] is audio
        assert kwargs["audio_sample_rate"] == 32000
        assert kwargs["fps"] == 24
        Path(kwargs["output_path"]).write_bytes(b"mock-mp4")

    monkeypatch.setenv("HF_HUB_OFFLINE", "0")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "0")
    monkeypatch.setattr(h3, "check_hardware", lambda: {"gpu": "test"})
    monkeypatch.setattr(h3, "load_pipeline", lambda workflow: pipeline)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", lambda: None)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda: 10 * 2**30)
    monkeypatch.setattr(export_utils, "encode_video", encode)
    h3.generate(args)
    metadata = json.loads(args.output.with_suffix(".json").read_text())
    assert metadata["ai_generated"] is True
    assert metadata["revision"] == h3.MODEL_REVISION
    assert metadata["frames"] == 124
    assert metadata["peak_allocated_vram_gib"] == 10


@pytest.mark.parametrize(
    "workflow,changes,forwarded",
    [
        (
            "fl2va",
            {"first_frame": Path("first.png"), "last_frame": Path("last.png")},
            {"image": object(), "last_image": object()},
        ),
        (
            "ref2va",
            {"reference_image": [Path("one.png"), Path("two.png")]},
            {"references": [object(), object()]},
        ),
    ],
)
def test_generate_forwards_image_arguments(
    tmp_path, monkeypatch, workflow, changes, forwarded
):
    import torch
    from diffusers.utils import export_utils

    args = options(tmp_path, **changes)
    captured = {}

    def pipeline(**kwargs):
        captured.update(kwargs)
        return {"videos": [object()], "audio": [object()], "sampling_rate": 32000}

    monkeypatch.setattr(h3, "image_inputs", lambda unused: forwarded)
    monkeypatch.setattr(torch.cuda, "reset_peak_memory_stats", lambda: None)
    monkeypatch.setattr(torch.cuda, "max_memory_allocated", lambda: 0)
    monkeypatch.setattr(export_utils, "encode_video", lambda *args, **kwargs: None)

    metadata = h3.generate(args, pipe=pipeline, hardware={"gpu": "test"})

    assert metadata["workflow"] == workflow
    assert {name: captured[name] for name in forwarded} == forwarded
    assert metadata["reference_image_count"] == len(forwarded.get("references", []))
    assert metadata["frame_roles"] == [
        name for name in ("image", "last_image") if name in forwarded
    ]


@pytest.mark.parametrize("workflow", ["t2va", "fl2va", "ref2va"])
def test_progress_preserves_workflow_denoiser_and_reports_successes(
    monkeypatch, workflow
):
    from diffusers import MiniMaxH3ModularPipeline

    events = []
    pipe = MiniMaxH3ModularPipeline(workflow=workflow)
    original = pipe.blocks.sub_blocks["denoise.denoise"]
    original_type = type(original)
    state = Namespace(timesteps=[1, 2, 3])

    def upstream(self, components, state, *, i, t):
        events.append(("executed", i))
        if i == 2:
            raise RuntimeError("step failed")
        return components, state

    monkeypatch.setattr(original_type, "loop_step", upstream)
    reporting_pipe = h3.attach_progress(
        pipe, lambda completed, total: events.append((completed, total))
    )
    reporting_block = reporting_pipe.blocks.sub_blocks["denoise.denoise"]
    assert type(reporting_block).__mro__[1] is original_type
    assert all(
        reporting_pipe.components[name] is component
        for name, component in pipe.components.items()
    )
    transformer_name = "transformer_ref" if workflow == "ref2va" else "transformer"
    assert transformer_name in reporting_pipe.components
    assert (
        reporting_pipe.components[transformer_name] is pipe.components[transformer_name]
    )
    reporting_block.loop_step(None, state, i=0, t=1)
    reporting_block.loop_step(None, state, i=1, t=2)
    with pytest.raises(RuntimeError, match="step failed"):
        reporting_block.loop_step(None, state, i=2, t=3)
    assert events == [
        (0, 3),
        ("executed", 0),
        (1, 3),
        ("executed", 1),
        (2, 3),
        ("executed", 2),
    ]
