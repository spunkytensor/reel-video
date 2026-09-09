# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Opt-in real GPU acceptance test: six normal-quality REST jobs, no mock inference."""

import argparse
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from provider import DURATIONS, MODEL, SIZES

PROMPT = (
    "A red fox walks slowly through a snowy pine forest at dawn. In one continuous "
    "shot, the camera tracks smoothly alongside the fox at its eye level. Its paws "
    "leave shallow prints in the snow and its fur moves gently in the breeze. "
    "Quiet wind passes through the pine branches with soft snow crunches beneath "
    "its paws. Natural lighting, realistic movement, no cuts, no speech, no music."
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Explicitly skip completed profiles from report.json; creates new jobs for remaining profiles.",
    )
    options = parser.parse_args()
    base = os.environ.get("REEL_VIDEO_BASE_URL", "http://127.0.0.1:8088")
    directory = Path("outputs/api-verification")
    directory.mkdir(parents=True, exist_ok=True)
    reports = (
        json.loads((directory / "report.json").read_text()) if options.resume else []
    )
    with httpx.Client(
        base_url=base,
        headers={"Authorization": "Bearer " + os.environ["REEL_VIDEO_API_TOKEN"]},
        timeout=30,
    ) as client:
        assert client.get("/api/v1/videos/models").status_code == 200
        for size in SIZES:
            for duration in DURATIONS:
                if any(
                    report["job"]["local"]["request"]["size"] == size
                    and report["job"]["local"]["request"]["duration"] == duration
                    for report in reports
                ):
                    continue
                payload = {
                    "model": MODEL,
                    "prompt": PROMPT,
                    "size": size,
                    "duration": duration,
                    "seed": 42,
                    "generate_audio": True,
                    "local": {"export": "1080p" if duration == 10 else "original"},
                }
                key = str(uuid.uuid4())
                started = time.monotonic()
                response = client.post(
                    "/api/v1/videos", json=payload, headers={"Idempotency-Key": key}
                )
                assert response.status_code == 202, response.text
                latency = time.monotonic() - started
                assert latency < 5
                job = response.json()
                duplicate = client.post(
                    "/api/v1/videos", json=payload, headers={"Idempotency-Key": key}
                )
                assert duplicate.json()["id"] == job["id"]
                print(
                    f"Accepted {job['id']} {size} {duration}s latency={latency:.3f}s",
                    flush=True,
                )
                transitions = [job["status"]]
                steps = []
                deadline = time.monotonic() + 3600
                while job["status"] not in ("completed", "failed"):
                    assert time.monotonic() < deadline, "Generation timed out"
                    time.sleep(5)
                    before = time.monotonic()
                    health = client.get("/health")
                    assert health.status_code == 200 and time.monotonic() - before < 5
                    job = client.get(job["polling_url"]).json()
                    if job["status"] != transitions[-1]:
                        transitions.append(job["status"])
                    progress = job["local"].get("progress")
                    if progress:
                        assert (
                            progress["unit"] == "denoising_steps"
                            and progress["total"] == 49
                        )
                        assert not steps or progress["completed"] >= steps[-1]
                        if not steps or progress["completed"] != steps[-1]:
                            steps.append(progress["completed"])
                    print(
                        f"{job['id']} {job['status']} {job['local']['phase']}",
                        flush=True,
                    )
                assert job["status"] == "completed", job
                assert "in_progress" in transitions
                assert len(steps) > 2 and steps[-1] == 49, steps
                url = job["unsigned_urls"][0]
                content = client.get(url)
                assert (
                    content.status_code == 200
                    and content.headers["content-type"] == "video/mp4"
                )
                partial = client.get(url, headers={"Range": "bytes=0-1023"})
                assert (
                    partial.status_code == 206
                    and partial.content == content.content[:1024]
                )
                assert httpx.get(url).status_code == 401
                target = directory / f"{size}-{duration}s.mp4"
                target.write_bytes(content.content)
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
                report = {
                    "job": job,
                    "post_latency_seconds": latency,
                    "transitions": transitions,
                    "observed_denoising_steps": steps,
                    "sha256": hashlib.sha256(content.content).hexdigest(),
                }
                reports.append(report)
                (directory / "report.json").write_text(json.dumps(reports, indent=2))
                print(
                    f"VERIFIED {size} {duration}s: {job['local']['metadata']}",
                    flush=True,
                )


if __name__ == "__main__":
    main()
