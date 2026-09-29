# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Apply narrowly reviewed source fixes without adding build tools to the image."""

from pathlib import Path

tarfile = Path("/usr/lib/python3.12/tarfile.py")
source = tarfile.read_text()
vulnerable = "                    os.link(tarinfo._link_target, targetpath)\n"
fixed = """                    # Resolve the target so the hard link points to the file
                    # itself. Otherwise os.link() may duplicate a symlink to a
                    # shallower location, where its relative target escapes the
                    # destination directory. (CVE-2026-82049)
                    os.link(os.path.realpath(tarinfo._link_target), targetpath)
"""
if source.count(vulnerable) != 1:
    raise SystemExit("unexpected tarfile.py source; refusing to apply security patch")
tarfile.write_text(source.replace(vulnerable, fixed))
# Wolfi precompiles the stdlib with unchecked-hash bytecode, so remove the old
# cache rather than allowing it to bypass the reviewed source replacement.
for bytecode in tarfile.parent.glob("__pycache__/tarfile*.pyc"):
    bytecode.unlink()
