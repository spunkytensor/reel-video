# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Single, supervised GPU owner; independent of the HTTP request loop."""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from argparse import Namespace
from functools import partial
from pathlib import Path

import h3
from provider import MODE_SWITCH_EXIT, JobCancelled, Store, VideoRequest, WorkflowSwitch

log = logging.getLogger(__name__)


class StopSignal:
    """Lock-free shared stop byte; an exiting worker cannot strand Event waiters.

    The supervisor owns writes, worker threads only poll. In particular, never
    share multiprocessing.Event.wait() with daemon threads that exit on handoff.
    """

    def __init__(self, flag):
        self.flag = flag

    def is_set(self):
        return bool(self.flag.value)

    def set(self):
        self.flag.value = 1

    def wait(self, seconds):
        if not self.is_set():
            time.sleep(seconds)
        return self.is_set()


def probe(path: Path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=180,
    )
    return json.loads(result.stdout)


def finalize(source: Path, request: VideoRequest, metadata: dict, started=None):
    """Encode/validate completely before atomically publishing final.mp4."""
    target = source.parent / "delivery.mp4"
    width, height = map(int, request.size.split("x"))
    if request.local.export == "1080p":
        if width > height:
            width, height = 1920, 1080
        elif height > width:
            width, height = 1080, 1920
        else:
            width, height = 1080, 1080
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                *(["-map", "0:a:0"] if request.generate_audio else []),
                "-vf",
                f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-pix_fmt",
                "yuv420p",
                *(["-c:a", "copy"] if request.generate_audio else []),
                "-movflags",
                "+faststart",
                "-threads",
                "8",
                str(target),
            ],
            check=True,
            capture_output=True,
            timeout=600,
        )
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-n",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                *(["-map", "0:a:0"] if request.generate_audio else []),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(target),
            ],
            check=True,
            capture_output=True,
            timeout=180,
        )
    measured = probe(target)
    video = next(s for s in measured["streams"] if s["codec_type"] == "video")
    audio = next((s for s in measured["streams"] if s["codec_type"] == "audio"), None)
    frames = h3.frame_count(request.duration)
    if (
        video["width"],
        video["height"],
        video["r_frame_rate"],
        int(video["nb_read_frames"]),
    ) != (width, height, "24/1", frames):
        raise ValueError(
            "Encoded dimensions, rate or frame count do not match the request."
        )
    if (
        video["codec_name"] != "h264"
        or request.generate_audio
        and (
            audio is None
            or audio["codec_name"] != "aac"
            or audio["sample_rate"] != "32000"
            or audio["channels"] != 2
        )
        or not request.generate_audio
        and audio is not None
    ):
        raise ValueError(
            "Encoded video/audio streams do not match the delivery contract."
        )
    if audio is not None and abs(float(audio["duration"]) - frames / 24) > 0.15:
        raise ValueError("Audio/video durations are out of sync.")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(target),
            "-f",
            "null",
            "-",
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    metadata.update(
        {
            "source_size": request.size,
            "delivery_size": f"{width}x{height}",
            "fit_policy": "pad" if request.local.export == "1080p" else "original",
            "scaling_method": "ffmpeg-lanczos"
            if request.local.export == "1080p"
            else "none",
            "upscaled": request.local.export != "original",
            "requested_duration": request.duration,
            "video_frame_duration": frames / 24,
            "container_duration": float(measured["format"]["duration"]),
            "audio_included": audio is not None,
        }
    )
    if audio is not None:
        metadata.update(
            {
                "audio_duration": float(audio["duration"]),
                "audio_sample_rate": 32000,
                "audio_channels": 2,
            }
        )
    if started is not None:
        metadata["elapsed_seconds_including_load"] = round(
            time.monotonic() - started, 2
        )
    # Fsync files before publishing execution state. No client can access intermediates.
    meta_path = source.parent / "final.json"
    with meta_path.open("x") as out:
        json.dump(metadata, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    with target.open("rb") as out:
        os.fsync(out.fileno())
    target.rename(source.parent / "final.mp4")
    directory = os.open(source.parent, os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    source.unlink()
    source.with_suffix(".json").unlink(missing_ok=True)
    return metadata


def isolate_network():
    """Linux user/network namespaces: inference and FFmpeg have no network routes."""
    # Fail closed before loading a model if namespace creation is unavailable.
    # Offline environment flags alone would not prevent arbitrary network calls.
    uid, gid = os.getuid(), os.getgid()
    os.unshare(os.CLONE_NEWUSER)
    Path("/proc/self/setgroups").write_text("deny")
    Path("/proc/self/uid_map").write_text(f"0 {uid} 1")
    Path("/proc/self/gid_map").write_text(f"0 {gid} 1")
    os.unshare(os.CLONE_NEWNET)


def run_worker(root, queue_limit, retention_days, quota_bytes, stop, parent_pid):
    isolate_network()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    store = Store(Path(root), queue_limit, retention_days, quota_bytes)

    def heartbeat():
        while not stop.wait(5):
            # A hard-killed HTTP parent must not leave an orphan retaining the GPU.
            if os.getppid() != parent_pid:
                os._exit(1)
            with store.connect() as db:
                db.execute(
                    "UPDATE worker SET updated=? WHERE singleton=1", (time.time(),)
                )

    threading.Thread(target=heartbeat, daemon=True).start()
    with h3.gpu_owner():
        store.reconcile()
        pipe, hardware = None, None
        profile = None
        store.worker_state("idle")
        last_cleanup = 0
        while not stop.is_set():
            if time.monotonic() - last_cleanup > 60:
                store.cleanup()
                last_cleanup = time.monotonic()
            try:
                job = store.claim(profile)
            except WorkflowSwitch:
                # No job was claimed. Exit the process to release every model,
                # pinned host buffer and CUDA allocation before loading another profile.
                store.worker_state("switching")
                raise SystemExit(MODE_SWITCH_EXIT) from None
            if not job:
                stop.wait(1)
                continue
            job_id = job["id"]
            started = time.monotonic()
            folder = store.root / "media" / job_id
            log.warning("job=%s started", job_id)
            try:
                store.raise_if_cancelled(job_id)
                request = VideoRequest.model_validate_json(job["request"])
                cold_start = pipe is None
                if cold_start:
                    store.worker_state("loading")
                    hardware = h3.check_hardware()
                    pipe = h3.load_pipeline(request.workflow)
                    profile = request.worker_profile
                store.raise_if_cancelled(job_id)
                store.phase(job_id, "conditioning" if request.images else "generating")
                store.worker_state("generating")
                folder.mkdir(exist_ok=False)
                images, provenance = store.materialize_images(request, folder)
                store.raise_if_cancelled(job_id)
                width, height = map(int, request.size.split("x"))
                metadata = h3.generate(
                    Namespace(
                        prompt=request.prompt,
                        seconds=request.duration,
                        width=width,
                        height=height,
                        seed=request.seed,
                        steps=request.steps,
                        output=folder / "source.mp4",
                        **images,
                    ),
                    pipe=pipe,
                    hardware=hardware,
                    on_progress=partial(store.progress, job_id),
                    on_phase=partial(store.phase, job_id),
                )
                store.raise_if_cancelled(job_id)
                store.phase(job_id, "encoding")
                store.worker_state("encoding")
                import psutil
                import torch

                metadata.update(
                    {
                        "cold_start": cold_start,
                        "conditioning_images": provenance,
                        "workflow": request.workflow,
                        "worker_pid": os.getpid(),
                        "worker_rss_gib": round(
                            psutil.Process().memory_info().rss / 2**30, 2
                        ),
                        "retained_allocated_vram_gib": round(
                            torch.cuda.memory_allocated() / 2**30, 2
                        ),
                        "reserved_vram_gib": round(
                            torch.cuda.memory_reserved() / 2**30, 2
                        ),
                    }
                )
                metadata = finalize(
                    folder / "source.mp4", request, metadata, started=started
                )
                store.raise_if_cancelled(job_id)
                if not store.finish(job_id, metadata=metadata):
                    raise JobCancelled()
                store.worker_state("ready")
                log.warning(
                    "job=%s completed elapsed=%.2f peak_vram=%s",
                    job_id,
                    time.monotonic() - started,
                    metadata["peak_allocated_vram_gib"],
                )
            except JobCancelled:
                log.warning("job=%s canceled", job_id)
                shutil.rmtree(folder, ignore_errors=True)
                store.finish_cancelled(job_id)
                # Hooks may have stopped mid-forward. Restart rather than reusing
                # a pipeline whose offload state may be incomplete.
                store.worker_state("unavailable")
                return
            except Exception as exc:
                # Restart the process after any execution failure; CUDA/OOM state may be poisoned.
                code = (
                    "out_of_memory"
                    if "OutOfMemory" in type(exc).__name__
                    else "generation_failed"
                )
                log.exception("job=%s failed type=%s", job_id, type(exc).__name__)
                finished = store.finish(
                    job_id,
                    error={
                        "code": code,
                        "message": "Local generation or encoding failed. Check worker resources and logs, then explicitly retry as a new job.",
                    },
                )
                if not finished:
                    shutil.rmtree(folder, ignore_errors=True)
                    store.finish_cancelled(job_id)
                store.worker_state("unavailable")
                return
        store.worker_state("stopped")
