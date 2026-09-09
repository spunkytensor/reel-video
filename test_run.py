# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Launcher checks with fake Docker: no containers or GPU work."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "short",
        "é" * 32,
        "replace-with-your-own-secret-of-at-least-32-characters",
    ],
)
def test_launcher_rejects_missing_or_invalid_key(tmp_path, token):
    shutil.copy(Path(__file__).with_name("run.sh"), tmp_path / "run.sh")
    if token is not None:
        (tmp_path / ".env").write_text(f"REEL_VIDEO_API_TOKEN={token}\n")
    result = subprocess.run(
        ["bash", str(tmp_path / "run.sh")], capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert "REEL_VIDEO_API_TOKEN" in result.stderr
    assert "Docker is required" not in result.stderr


@pytest.mark.parametrize("retention,quota", [(None, None), (5, 40)])
def test_launcher_passes_env_key_without_logging_it(tmp_path, retention, quota):
    shutil.copy(Path(__file__).with_name("run.sh"), tmp_path / "run.sh")
    token = "test-only-credential-" * 3
    (tmp_path / ".env").write_text(
        f"REEL_VIDEO_API_TOKEN={token}\nREEL_VIDEO_NETWORK_HOSTS=gpu.example.internal\n"
        + (
            f"REEL_VIDEO_RETENTION_DAYS={retention}\nREEL_VIDEO_QUOTA_GIB={quota}\n"
            if retention
            else ""
        )
    )
    docker = tmp_path / "docker"
    docker.write_text("""#!/bin/bash
case "$1" in
  info|build) exit 0 ;;
  run)
    [[ "$REEL_VIDEO_API_TOKEN" == "$EXPECTED_KEY" ]] || exit 2
    [[ " $* " == *" --env REEL_VIDEO_API_TOKEN "* ]] || exit 3
    [[ "$*" != *"$EXPECTED_KEY"* ]] || exit 4
    [[ " $* " == *" --env REEL_VIDEO_RETENTION_DAYS=$EXPECTED_RETENTION "* ]] || exit 5
    [[ " $* " == *" --env REEL_VIDEO_QUOTA_GIB=$EXPECTED_QUOTA "* ]] || exit 6
    [[ " $* " == *" --env REEL_VIDEO_NETWORK_HOSTS=gpu.example.internal "* ]] || exit 7
    ;;
  logs) printf 'Reel Video is starting in the background.\\nStudio (this computer): http://127.0.0.1:8088\\nModel weights: cached\\n' ;;
  *) exit 5 ;;
esac
""")
    docker.chmod(0o755)
    result = subprocess.run(
        ["bash", str(tmp_path / "run.sh")],
        capture_output=True,
        check=False,
        text=True,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "EXPECTED_KEY": token,
            "REEL_VIDEO_RETENTION_DAYS": "",
            "REEL_VIDEO_QUOTA_GIB": "",
            "EXPECTED_RETENTION": str(retention or 3),
            "EXPECTED_QUOTA": str(quota or 25),
        },
    )
    assert result.returncode == 0, result.stderr
    assert token not in result.stdout + result.stderr
    assert "Studio (this computer)" in result.stdout


@pytest.mark.parametrize(
    ("configuration", "environment_name", "expected", "containers", "failure"),
    [
        (None, "", "reel-video", "unrelated\nreel-video", ""),
        ("REEL_VIDEO_CONTAINER_NAME=custom\n", "other", "custom", "custom", ""),
        (None, "custom", "custom", "custom", ""),
        (None, "", "reel-video", "reel-video-other", ""),
        (None, "", "reel-video", "reel-video", "info"),
        (None, "", "reel-video", "reel-video", "container"),
        (None, "", "reel-video", "reel-video", "stop"),
    ],
)
def test_stop_targets_configured_container(
    tmp_path, configuration, environment_name, expected, containers, failure
):
    shutil.copy(Path(__file__).with_name("stop.sh"), tmp_path / "stop.sh")
    if configuration is not None:
        (tmp_path / ".env").write_text(configuration)
    docker = tmp_path / "docker"
    docker.write_text("""#!/bin/bash
[[ "$1" != "$FAIL_COMMAND" ]] || exit 1
case "$1" in
  info) exit 0 ;;
  container)
    [[ "$*" == "container ls --all --format {{.Names}}" ]] || exit 2
    printf '%s\\n' "$CONTAINERS"
    ;;
  stop)
    [[ "$*" == "stop --timeout 25 -- $EXPECTED_NAME" ]] || exit 3
    printf '%s' "$5" > "$STOP_RECORD"
    ;;
  *) exit 4 ;;
esac
""")
    docker.chmod(0o755)
    record = tmp_path / "stopped"
    result = subprocess.run(
        ["bash", str(tmp_path / "stop.sh")],
        cwd="/tmp",
        capture_output=True,
        text=True,
        check=False,
        env=os.environ
        | {
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "REEL_VIDEO_CONTAINER_NAME": environment_name,
            "EXPECTED_NAME": expected,
            "CONTAINERS": containers,
            "FAIL_COMMAND": failure,
            "STOP_RECORD": str(record),
        },
    )
    if failure:
        assert result.returncode != 0
        assert "already stopped" not in result.stdout
        assert not record.exists()
    elif expected in containers.splitlines():
        assert result.returncode == 0, result.stderr
        assert record.read_text() == expected
        assert "volumes are retained" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert "already stopped" in result.stdout
        assert not record.exists()
