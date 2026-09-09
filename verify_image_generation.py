# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Opt-in offline GPU validation; requires an available GPU lease, never submits API jobs."""

import argparse
import hashlib
import json
import resource
import subprocess
import time
from pathlib import Path

import h3
from worker import isolate_network, probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=["first", "last", "both", "reference", "keyframes"],
        required=True,
    )
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--last-image", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--steps", type=int, default=50)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if args.runs < 1 or (
        args.mode in ("both", "keyframes") and args.last_image is None
    ):
        parser.error(
            "Positive runs required; both/keyframes modes also require --last-image."
        )
    modes = (
        ["first", "last", "both"] if args.mode == "keyframes" else [args.mode]
    ) * args.runs
    prompt = (
        "The same red fox walks slowly through the snowy pine forest at dawn. "
        "One continuous eye-level tracking shot follows its movement. Preserve its "
        "red fur, white chest, dark legs and the quiet winter setting. Gentle wind "
        "and soft snow crunches, no cuts, no speech, no music."
    )
    options = argparse.Namespace(
        prompt=prompt,
        seconds=5,
        width=960,
        height=544,
        seed=42,
        steps=args.steps,
        first_frame=args.image if args.mode in ("first", "both", "keyframes") else None,
        last_frame=(args.last_image if args.mode == "both" else args.image)
        if args.mode in ("last", "both")
        else None,
        reference_image=[args.image] if args.mode == "reference" else [],
        output=args.output_dir / "run-1.mp4",
    )
    h3.validate_generation(options)
    if args.output_dir.exists():
        parser.error(
            "Use a new output directory; existing validation evidence is preserved."
        )
    with h3.gpu_owner():
        # Must happen before CUDA imports or background sampling threads.
        isolate_network()
        import psutil
        import torch

        args.output_dir.mkdir(parents=True)
        hardware = h3.check_hardware()
        load_started = time.monotonic()
        pipe = h3.load_pipeline(h3.generation_workflow(options))
        load_seconds = time.monotonic() - load_started
        report = {
            "mode": args.mode,
            "revision": h3.MODEL_REVISION,
            "hardware": hardware,
            "load_seconds": load_seconds,
            "offline_network_namespace": True,
            "input_sha256": hashlib.sha256(args.image.read_bytes()).hexdigest(),
            "last_input_sha256": hashlib.sha256(
                args.last_image.read_bytes()
            ).hexdigest()
            if args.last_image
            else None,
            "runs": [],
        }
        for run, mode in enumerate(modes, 1):
            options.output = args.output_dir / f"run-{run}.mp4"
            options.first_frame = args.image if mode in ("first", "both") else None
            options.last_frame = (
                (args.last_image if mode == "both" else args.image)
                if mode in ("last", "both")
                else None
            )
            events = []

            def progress(completed, total, events=events, run=run):
                events.append(completed)
                print(
                    json.dumps({"run": run, "completed": completed, "total": total}),
                    flush=True,
                )

            metadata = h3.generate(
                options, pipe=pipe, hardware=hardware, on_progress=progress
            )
            assert events == list(range(args.steps)), events
            media = probe(options.output)
            video = next(s for s in media["streams"] if s["codec_type"] == "video")
            audio = next(s for s in media["streams"] if s["codec_type"] == "audio")
            assert (video["width"], video["height"], int(video["nb_read_frames"])) == (
                960,
                544,
                124,
            )
            assert video["r_frame_rate"] == "24/1"
            assert audio["sample_rate"] == "32000" and audio["channels"] == 2
            subprocess.run(
                [
                    "ffmpeg",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(options.output),
                    "-f",
                    "null",
                    "-",
                ],
                check=True,
                timeout=180,
            )
            report["runs"].append(
                {
                    "input_mode": mode,
                    "metadata": metadata,
                    "media": media,
                    "worker_rss_gib": psutil.Process().memory_info().rss / 2**30,
                    "peak_host_rss_gib": resource.getrusage(
                        resource.RUSAGE_SELF
                    ).ru_maxrss
                    / 2**20,
                    "retained_allocated_vram_gib": torch.cuda.memory_allocated()
                    / 2**30,
                    "reserved_vram_gib": torch.cuda.memory_reserved() / 2**30,
                    "measured_progress": events,
                }
            )
            (args.output_dir / "report.json").write_text(
                json.dumps(report, indent=2) + "\n"
            )
            print(
                f"PASS run {run}: full decode, dimensions, frames, stereo audio, real step progress",
                flush=True,
            )


if __name__ == "__main__":
    main()
