# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
import threading

import pytest

import worker
from image_codec import DecodedImage
from provider import IMAGE_PATH, MODEL, Store, VideoRequest


def video_request(
    export="original", prompt="test", generate_audio=True, size="960x544"
):
    return VideoRequest(
        model=MODEL,
        prompt=prompt,
        duration=5,
        size=size,
        generate_audio=generate_audio,
        local={"export": export},
    )


def ffprobe(path, *entries):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-of",
            "json",
            *entries,
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def make_source(path, size="960x544"):
    # Bound both inputs, rather than stopping output with -frames:v/-shortest:
    # older FFmpeg can stop audio early while delayed H.264 frames are flushed.
    duration = 124 / 24
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={size}:rate=24:duration={duration:.9f}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=997:sample_rate=32000:duration={duration:.9f}",
            "-ac",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=180,
    )


def audio_packet_hashes(path):
    data = ffprobe(
        path,
        "-select_streams",
        "a:0",
        "-show_packets",
        "-show_data_hash",
        "sha256",
    )
    return [packet["data_hash"] for packet in data["packets"]]


@pytest.mark.parametrize(
    "export,fit",
    [
        ("original", "original"),
        ("1080p", "pad"),
    ],
)
@pytest.mark.parametrize("generate_audio", [True, False])
@pytest.mark.parametrize(
    "size,upscaled",
    [
        ("960x544", (1920, 1080)),
        ("544x960", (1080, 1920)),
        ("768x768", (1080, 1080)),
    ],
)
def test_cpu_ffmpeg_finalize_contract(
    tmp_path, export, fit, generate_audio, size, upscaled
):
    dimensions = upscaled if export == "1080p" else tuple(map(int, size.split("x")))
    folder = tmp_path / export
    folder.mkdir()
    source = folder / "source.mp4"
    make_source(source, size)
    source_streams = worker.probe(source)["streams"]
    source_video = next(s for s in source_streams if s["codec_type"] == "video")
    source_audio = next(s for s in source_streams if s["codec_type"] == "audio")
    assert int(source_video["nb_read_frames"]) == 124
    assert float(source_video["duration"]) == pytest.approx(124 / 24, abs=0.001)
    assert float(source_audio["duration"]) == pytest.approx(124 / 24, abs=0.001)
    before_audio = audio_packet_hashes(source)
    metadata = worker.finalize(
        source,
        video_request(export, generate_audio=generate_audio, size=size),
        {"marker": 1},
    )
    final = folder / "final.mp4"
    assert final.exists() and not source.exists()
    assert json.loads((folder / "final.json").read_text()) == metadata
    measured = worker.probe(final)
    video = next(s for s in measured["streams"] if s["codec_type"] == "video")
    audio = next((s for s in measured["streams"] if s["codec_type"] == "audio"), None)
    assert (video["width"], video["height"]) == dimensions
    assert video["codec_name"] == "h264"
    assert video["r_frame_rate"] == "24/1"
    assert int(video["nb_read_frames"]) == 124
    if generate_audio:
        assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == (
            "aac",
            "32000",
            2,
        )
        assert audio_packet_hashes(final) == before_audio
    else:
        assert audio is None
    assert metadata["source_size"] == size
    assert metadata["delivery_size"] == f"{dimensions[0]}x{dimensions[1]}"
    assert metadata["fit_policy"] == fit
    assert metadata["audio_included"] is generate_audio
    subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(final), "-f", "null", "-"],
        check=True,
        capture_output=True,
        timeout=180,
    )


def test_invalid_source_is_never_published(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not a media file")
    with pytest.raises(subprocess.CalledProcessError):
        worker.finalize(source, video_request(), {})
    assert not (tmp_path / "final.mp4").exists()
    assert not (tmp_path / "final.json").exists()


def test_worker_reuses_warm_pipeline(tmp_path, monkeypatch):
    store = Store(tmp_path)
    for prompt in ("one", "two"):
        store.submit(video_request(prompt=prompt), None)
    stop = threading.Event()
    pipeline = object()
    calls = {"load": 0, "generate": [], "finish": []}

    # contextlib.nullcontext is used explicitly to avoid loading CUDA/model machinery.
    from contextlib import nullcontext

    monkeypatch.setattr(worker, "isolate_network", lambda: None)
    monkeypatch.setattr(worker.h3, "gpu_owner", nullcontext)
    monkeypatch.setattr(worker.h3, "check_hardware", lambda: {"cpu": True})

    def load(workflow):
        assert workflow == "t2va"
        calls["load"] += 1
        return pipeline

    def generate(args, pipe, hardware, on_progress=None, on_phase=None):
        assert args.steps == 50
        calls["generate"].append((pipe, hardware, args.prompt))
        args.output.write_bytes(b"mock")
        return {"peak_allocated_vram_gib": 0}

    def finalize(_source, _request, metadata, started=None):
        calls["finish"].append(metadata["cold_start"])
        if len(calls["finish"]) == 2:
            stop.set()
        return metadata

    monkeypatch.setattr(worker.h3, "load_pipeline", load)
    monkeypatch.setattr(worker.h3, "generate", generate)
    monkeypatch.setattr(worker, "finalize", finalize)
    monkeypatch.setattr(worker.os, "getppid", lambda: 123)
    worker.run_worker(str(tmp_path), 4, 7, 10 * 2**30, stop, 123)

    assert calls["load"] == 1
    assert [item[0] for item in calls["generate"]] == [pipeline, pipeline]
    assert calls["finish"] == [True, False]
    assert [row["status"] for row in Store(tmp_path).recent()] == [
        "completed",
        "completed",
    ]


def test_worker_cold_workflow_handoffs_preserve_and_condition_jobs(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    store = Store(tmp_path)
    image_data = {
        "first": (b"first-image", "1" * 64, 31, 21),
        "last": (b"last-image", "2" * 64, 32, 22),
        "reference-0": (b"reference-zero", "3" * 64, 33, 23),
        "reference-1": (b"reference-one", "4" * 64, 34, 24),
    }
    images = {
        name: store.save_image(DecodedImage(data, digest, width, height))
        for name, (data, digest, width, height) in image_data.items()
    }

    def image_url(name):
        return {
            "type": "image_url",
            "image_url": {"url": IMAGE_PATH + images[name]["id"]},
        }

    def request(prompt, steps, **images):
        return VideoRequest(
            model=MODEL,
            prompt=prompt,
            frame_images=images.get("frame_images", []),
            input_references=images.get("input_references", []),
            provider={
                "options": {"local": {"parameters": {"num_inference_steps": steps}}}
            },
        )

    jobs = [
        store.submit(
            request(
                "frames",
                7,
                frame_images=[
                    image_url("last") | {"frame_type": "last_frame"},
                    image_url("first") | {"frame_type": "first_frame"},
                ],
            ),
            None,
        ),
        store.submit(
            request(
                "references",
                11,
                input_references=[image_url("reference-0"), image_url("reference-1")],
            ),
            None,
        ),
        store.submit(request("text", 13), None),
    ]
    loads = []
    generations = []
    active_stop = None
    expected_pid = os.getpid()

    monkeypatch.setattr(worker, "isolate_network", lambda: None)
    monkeypatch.setattr(worker.h3, "gpu_owner", nullcontext)
    monkeypatch.setattr(worker.h3, "check_hardware", lambda: {"fake": True})
    monkeypatch.setattr(worker.os, "getppid", lambda: 123)

    def load(workflow):
        loads.append(workflow)
        return {"workflow": workflow}

    def generate(args, pipe, hardware, on_progress=None, on_phase=None):
        assert hardware == {"fake": True}
        assert pipe == {"workflow": loads[-1]}
        paths = {
            "first_frame": args.first_frame,
            "last_frame": args.last_frame,
            "reference_image": args.reference_image,
        }
        generations.append((args.prompt, args.steps, paths))
        args.output.write_bytes(b"mock-video")
        return {"peak_allocated_vram_gib": 0}

    def finalize(source, request, metadata, started=None):
        assert source.read_bytes() == b"mock-video"
        if request.workflow == "t2va":
            active_stop.set()
        return metadata

    monkeypatch.setattr(worker.h3, "load_pipeline", load)
    monkeypatch.setattr(worker.h3, "generate", generate)
    monkeypatch.setattr(worker, "finalize", finalize)

    def invoke(switches):
        nonlocal active_stop
        active_stop = threading.Event()
        try:
            if switches:
                with pytest.raises(SystemExit) as error:
                    worker.run_worker(str(tmp_path), 4, 7, 10 * 2**30, active_stop, 123)
                assert error.value.code == 75
            else:
                worker.run_worker(str(tmp_path), 4, 7, 10 * 2**30, active_stop, 123)
        finally:
            # Ensure each invocation's daemon heartbeat exits, including handoffs.
            active_stop.set()

    invoke(switches=True)
    assert store.get(jobs[1]["id"])["status"] == "pending"
    assert store.get(jobs[2]["id"])["status"] == "pending"
    invoke(switches=True)
    assert store.get(jobs[2]["id"])["status"] == "pending"
    invoke(switches=False)

    assert loads == ["fl2va", "ref2va", "t2va"]
    assert [(prompt, steps) for prompt, steps, _ in generations] == [
        ("frames", 7),
        ("references", 11),
        ("text", 13),
    ]
    frame_paths = generations[0][2]
    assert frame_paths["last_frame"].name == "input-0.png"
    assert frame_paths["last_frame"].read_bytes() == image_data["last"][0]
    assert frame_paths["first_frame"].name == "input-1.png"
    assert frame_paths["first_frame"].read_bytes() == image_data["first"][0]
    assert frame_paths["reference_image"] == []
    reference_paths = generations[1][2]
    assert reference_paths["first_frame"] is None
    assert reference_paths["last_frame"] is None
    assert [path.name for path in reference_paths["reference_image"]] == [
        "input-0.png",
        "input-1.png",
    ]
    assert [path.read_bytes() for path in reference_paths["reference_image"]] == [
        image_data["reference-0"][0],
        image_data["reference-1"][0],
    ]
    assert generations[2][2] == {
        "first_frame": None,
        "last_frame": None,
        "reference_image": [],
    }

    expected_provenance = [
        [
            (images["last"]["id"], "last_frame", 0, 32, 22),
            (images["first"]["id"], "first_frame", 1, 31, 21),
        ],
        [
            (images["reference-0"]["id"], "reference", 0, 33, 23),
            (images["reference-1"]["id"], "reference", 1, 34, 24),
        ],
        [],
    ]
    for job, workflow, provenance in zip(
        jobs, ["fl2va", "ref2va", "t2va"], expected_provenance, strict=True
    ):
        row = store.get(job["id"])
        metadata = json.loads(row["metadata"])
        assert row["status"] == "completed"
        assert metadata["workflow"] == workflow
        assert metadata["cold_start"] is True
        assert metadata["worker_pid"] == expected_pid
        assert [
            (item["id"], item["role"], item["index"], item["width"], item["height"])
            for item in metadata["conditioning_images"]
        ] == provenance


def test_worker_network_namespace_blocks_external_routes():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import socket
from worker import isolate_network
isolate_network()
try:
    socket.create_connection(('192.0.2.1', 443), timeout=1)
except OSError:
    print('network blocked')
else:
    raise AssertionError('Worker unexpectedly has a network route')
""",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "network blocked"


def test_oom_fails_only_current_job_and_restarts_cleanly(tmp_path, monkeypatch):
    from contextlib import nullcontext

    import torch

    store = Store(tmp_path)
    first = store.submit(video_request(prompt="one"), None)
    second = store.submit(video_request(prompt="two"), None)
    stop = threading.Event()
    monkeypatch.setattr(worker, "isolate_network", lambda: None)
    monkeypatch.setattr(worker.h3, "gpu_owner", nullcontext)
    monkeypatch.setattr(worker.h3, "check_hardware", dict)
    monkeypatch.setattr(worker.h3, "load_pipeline", lambda workflow: object())

    def fail(*args, **kwargs):
        raise torch.OutOfMemoryError("private debug details")

    monkeypatch.setattr(worker.h3, "generate", fail)
    try:
        worker.run_worker(str(tmp_path), 4, 7, 10 * 2**30, stop, 123)
    finally:
        stop.set()
    row = store.get(first["id"])
    assert row["status"] == "failed"
    assert json.loads(row["error"])["code"] == "out_of_memory"
    assert "private debug details" not in row["error"]
    assert store.get(second["id"])["status"] == "pending"
    store.cleanup()
    assert not (tmp_path / "media" / first["id"]).exists()


def test_running_job_cancels_at_progress_boundary_and_restarts_cleanly(
    tmp_path, monkeypatch
):
    from contextlib import nullcontext

    store = Store(tmp_path)
    first = store.submit(video_request(prompt="cancel me"), None)
    second = store.submit(video_request(prompt="keep me"), None)
    stop = threading.Event()
    monkeypatch.setattr(worker, "isolate_network", lambda: None)
    monkeypatch.setattr(worker.h3, "gpu_owner", nullcontext)
    monkeypatch.setattr(worker.h3, "check_hardware", dict)
    monkeypatch.setattr(worker.h3, "load_pipeline", lambda workflow: object())

    def generate(*args, on_progress, **kwargs):
        store.cancel_job(first["id"])
        on_progress(1, 49)
        pytest.fail("generation continued after cancellation")

    monkeypatch.setattr(worker.h3, "generate", generate)
    worker.run_worker(str(tmp_path), 4, 7, 10 * 2**30, stop, 123)

    canceled = store.get(first["id"])
    assert canceled["status"] == "failed"
    assert canceled["phase"] == "canceled"
    assert json.loads(canceled["error"])["code"] == "job_canceled"
    assert not (tmp_path / "media" / first["id"]).exists()
    assert store.get(second["id"])["status"] == "pending"


def _exit_with_stop_waiter(stop):
    import time

    threading.Thread(target=stop.wait, args=(60,), daemon=True).start()
    time.sleep(0.1)


def test_stop_signal_survives_exited_worker_waiters():
    # Exercise real spawn and abrupt daemon-thread exit with a bounded outer timeout.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import multiprocessing
from worker import StopSignal
from test_worker import _exit_with_stop_waiter
context = multiprocessing.get_context('spawn')
for _ in range(2):
    stop = StopSignal(context.RawValue('b', 0))
    process = context.Process(target=_exit_with_stop_waiter, args=(stop,))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    stop.set()
    assert stop.is_set() and stop.wait(0)
print('stop remains usable after worker exits')
""",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.stdout.strip() == "stop remains usable after worker exits"
