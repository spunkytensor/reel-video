# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Check original source headers, including new nonignored files before staging."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_SUFFIXES = {
    ".py",
    ".sh",
    ".cjs",
    ".js",
    ".mjs",
    ".ts",
    ".tsx",
    ".html",
    ".css",
    ".yml",
    ".yaml",
}


def main():
    # Git's inventory avoids descending into model caches, virtualenvs or media.
    # Binary assets, documentation and generated JSON locks are not source code;
    # third-party font license texts must retain their own licenses unchanged.
    paths = (
        subprocess.check_output(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    failures = []
    checked = 0
    for name in sorted(set(paths) - {""}):
        path = ROOT / name
        if not path.is_file() or not (
            path.suffix in SOURCE_SUFFIXES or path.name == "Dockerfile"
        ):
            continue
        checked += 1
        header = "\n".join(path.read_text().splitlines()[:8])
        if not all(
            text in header
            for text in (
                "Copyright 2026 Spunky Tensor",
                "SPDX-License-Identifier: Apache-2.0",
            )
        ):
            failures.append(name)
    if failures:
        raise SystemExit(
            "Missing original-source license header: " + ", ".join(failures)
        )
    print(f"SPDX headers verified in {checked} source files.")


if __name__ == "__main__":
    main()
