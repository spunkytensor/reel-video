# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Opt-in real image REST/GPU verification; enqueues four sequential jobs."""

import argparse
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from provider import MODEL
from verify_api import PROMPT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", type=Path, required=True)
    parser.add_argument("--last", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--reference-steps",
        type=int,
        choices=range(2, 101),
        default=50,
        help="Default 50; use 2 for a quicker plumbing-only repeat.",
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    reports = []
    with httpx.Client(
        base_url=os.environ.get("REEL_VIDEO_BASE_URL", "http://127.0.0.1:8088"),
        headers={"Authorization": "Bearer " + os.environ["REEL_VIDEO_API_TOKEN"]},
        timeout=30,
    ) as client:
        images = []
        for path in (args.first, args.last):
            response = client.post(
                "/api/v1/videos/images",
                content=path.read_bytes(),
                headers={"Content-Type": "image/png"},
            )
            assert response.status_code == 201, response.text
            images.append(
                {"type": "image_url", "image_url": {"url": response.json()["url"]}}
            )
        profiles = [
            (
                "ref2va",
                args.reference_steps,
                "960x544",
                5,
                {"input_references": images},
            ),
            ("ref2va", 2, "960x544", 5, {"input_references": images}),
            ("ref2va", 2, "768x768", 10, {"input_references": images}),
            (
                "fl2va",
                30,
                "960x544",
                5,
                {
                    "frame_images": [
                        images[0] | {"frame_type": "first_frame"},
                        images[1] | {"frame_type": "last_frame"},
                    ]
                },
            ),
        ]
        for index, (workflow, steps, size, duration, conditioning) in enumerate(
            profiles
        ):
            payload = {
                "model": MODEL,
                "prompt": PROMPT,
                "size": size,
                "duration": duration,
                "seed": 42 + index,
                "provider": {
                    "options": {"local": {"parameters": {"num_inference_steps": steps}}}
                },
                **conditioning,
            }
            key = str(uuid.uuid4())
            response = client.post(
                "/api/v1/videos", json=payload, headers={"Idempotency-Key": key}
            )
            assert response.status_code == 202, response.text
            job = response.json()
            (args.output_dir / f"accepted-{index}.json").write_text(
                json.dumps(job, indent=2)
            )
            replay = client.post(
                "/api/v1/videos", json=payload, headers={"Idempotency-Key": key}
            )
            assert replay.json()["id"] == job["id"]
            deadline = time.monotonic() + 3600
            observed = []
            phases = []
            while job["status"] not in ("completed", "failed"):
                assert time.monotonic() < deadline, "Generation timed out"
                time.sleep(5)
                before = time.monotonic()
                response = client.get(job["polling_url"])
                response.raise_for_status()
                assert time.monotonic() - before < 5, "API became unresponsive"
                job = response.json()
                phase = job["local"]["phase"]
                if not phases or phases[-1] != phase:
                    phases.append(phase)
                    print(job["id"], phase, flush=True)
                progress = job["local"].get("progress")
                if progress:
                    assert progress["total"] == steps - 1
                    assert not observed or progress["completed"] >= observed[-1]
                    if not observed or observed[-1] != progress["completed"]:
                        observed.append(progress["completed"])
                        print("progress", progress, flush=True)
            (args.output_dir / f"job-{index}.json").write_text(
                json.dumps(job, indent=2)
            )
            assert job["status"] == "completed", job
            assert observed and observed[-1] == steps - 1
            metadata = job["local"]["metadata"]
            assert metadata["workflow"] == workflow
            assert len(metadata["conditioning_images"]) == 2
            if reports:
                prior = reports[-1]["job"]["local"]["metadata"]
                same_profile = index == 1  # Only the seed/steps-only repeat stays warm.
                assert (prior["worker_pid"] == metadata["worker_pid"]) == same_profile
                assert metadata["cold_start"] == (not same_profile)
            response = client.get(job["unsigned_urls"][0])
            response.raise_for_status()
            target = args.output_dir / f"{index}-{workflow}.mp4"
            target.write_bytes(response.content)
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
                timeout=300,
            )
            reports.append({"job": job, "observed_steps": observed, "phases": phases})
            (args.output_dir / "report.json").write_text(json.dumps(reports, indent=2))
            print("VERIFIED", job["id"], metadata, flush=True)


if __name__ == "__main__":
    main()
