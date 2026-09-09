# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Fail rather than claim a clean audit of an incomplete dependency inventory."""

import json
import re
import sys
from pathlib import Path


def normalized(name):
    return re.sub(r"[-_.]+", "-", name).lower()


sbom = json.loads(Path(sys.argv[1]).read_text())
packages = sbom["artifacts"]
installed = {(normalized(p["name"]), p["version"]) for p in packages}
expected = {
    (normalized(name), version)
    for name, version in (
        line.split("==")
        for line in Path("requirements.txt").read_text().splitlines()
        if line and not line.startswith("#")
    )
}
if sys.argv[2] == "source":
    lock = json.loads(Path("package-lock.json").read_text())
    expected.update(
        (
            normalized(value.get("name", key.rsplit("node_modules/", 1)[-1])),
            value["version"],
        )
        for key, value in lock["packages"].items()
        if key
    )
else:
    assert sbom["distro"]["id"] == "wolfi", "Missing Wolfi OS inventory"
    expected.update(
        {
            (normalized("python-3.12-base"), "3.12.14-r6"),
            ("libexpat1", "2.8.4-r0"),
            ("ffmpeg", "9.0.1"),
        }
    )
    ffmpeg_versions = {p["version"] for p in packages if p["name"] == "ffmpeg"}
    assert ffmpeg_versions == {"9.0.1"}, f"Unexpected FFmpeg copies: {ffmpeg_versions}"

missing = expected - installed
if missing:
    raise SystemExit(f"Incomplete SBOM: missing locked packages {sorted(missing)}")
print(
    f"Verified {len(expected)} locked packages in {sys.argv[2]} SBOM ({len(packages)} artifacts)"
)
