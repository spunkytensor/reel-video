# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Run inside the actual image, offline and without GPU access or model weights."""

import hashlib
import io
import os
import ssl
import subprocess
import sys
import tarfile
from http.cookies import CookieError, Morsel, SimpleCookie
from pathlib import Path
from xml.parsers import expat

import av
import pytest
import torch
from PIL import Image


def test_python_hashlib_and_seeded_pip_compatibility(tmp_path):
    # OpenSSL/Expat ABI mismatches can break ensurepip despite a working ssl
    # import. Exercise the real bootstrap, including Wolfi's vendored fixes.
    assert hashlib.sha224(b"test").hexdigest() == (
        "90a3ed9e32b2aaf4c61c410eb925426119e1a9dc53d4286ade99a809"
    )
    environment = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True)
    for python in (sys.executable, str(environment / "bin/python")):
        subprocess.run(
            [
                python,
                "-c",
                (
                    "import pip; from pip._vendor import msgpack, certifi; "
                    "assert pip.__version__ == '26.2.2'; "
                    "assert msgpack.__version__ == '1.2.3'; "
                    "assert certifi.__version__ == '2026.10.23'"
                ),
            ],
            check=True,
        )


def test_python_ssl_contains_compromised_key_denylist():
    # CVE-2026-115557: CPython 3.12.16's native denylist matches SPKI hashes,
    # not root-store contents. Check the compiled eMudhra G3B entry from
    # CPython v3.12.16 Modules/_ssl/distrusted_spki.h, without private-key fixtures.
    assert (
        bytes.fromhex(
            "43bffa19d6ef651451f6841e019931e96f6f2eaeff28ab4b6f1ce4576de2717b"
        )
        in Path(ssl._ssl.__file__).read_bytes()
    )


def test_python_cookie_control_character_regression():
    # CVE-2026-3644: use the real stdlib, not a patched cookie implementation.
    for control in ("\r", "\n", "\x00"):
        morsel = Morsel()
        morsel.set("key", "value", "value")
        with pytest.raises(CookieError):
            morsel.update({"path": control})
        with pytest.raises(CookieError):
            morsel |= {"path": control}
        # A crafted/deserialized morsel must also be rejected at the output edge.
        dict.__setitem__(morsel, "path", control)
        cookie = SimpleCookie()
        cookie["key"] = morsel
        with pytest.raises(CookieError):
            cookie.output()
        with pytest.raises(CookieError):
            cookie.js_output()


def test_python_nested_dtd_has_recursion_guard():
    # CVE-2026-4224: a vulnerable C extension can crash, so isolate the parser.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
from xml.parsers import expat
parser = expat.ParserCreate()
parser.ElementDeclHandler = lambda *args: None
data = b'<!DOCTYPE root [<!ELEMENT root ' + b'(a,' * 50000 + b'a' + b')' * 50000 + b'>]><root/>'
try:
    parser.Parse(data, True)
except RecursionError:
    pass
else:
    raise AssertionError('missing native recursion guard')
""",
        ],
        check=True,
        timeout=30,
    )


def test_python_expat_uses_full_width_hash_salt():
    # CVE-2026-7210 requires BOTH the CPython patch and Expat >= 2.8.0.
    # This checks the actual binary's API reference, not only a version label.
    import pyexpat

    assert sys.version_info[:3] == (3, 12, 16)
    assert expat.version_info >= (2, 9, 0)
    symbols = subprocess.check_output(["readelf", "-Ws", pyexpat.__file__], text=True)
    assert "XML_SetHashSalt16Bytes" in symbols
    dependencies = subprocess.check_output(
        ["readelf", "-d", pyexpat.__file__], text=True
    )
    assert "libexpat.so.1" in dependencies
    # These stdlib regressions were validated against this exact APK revision.
    subprocess.run(
        ["apk", "--no-network", "info", "--installed", "python-3.12=3.12.16-r3"],
        check=True,
    )


def test_python_tarfile_hardlink_to_symlink_is_not_relocated(tmp_path):
    # CVE-2026-82049: a relocated hard link must resolve to the in-tree file,
    # not duplicate the symlink where its relative target can escape.
    archive = tmp_path / "crafted.tar"
    with tarfile.open(archive, "w") as output:
        regular = tarfile.TarInfo("a/escape")
        regular.size = len(b"decoy")
        output.addfile(regular, io.BytesIO(b"decoy"))
        symlink = tarfile.TarInfo("a/b/s")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = os.path.join("..", "escape")
        output.addfile(symlink)
        hardlink = tarfile.TarInfo("s")
        hardlink.type = tarfile.LNKTYPE
        hardlink.linkname = "a/b/s"
        output.addfile(hardlink)
    destination = tmp_path / "output"
    with tarfile.open(archive) as source:
        source.extractall(destination, filter="data")
    assert not (destination / "s").is_symlink()
    assert (destination / "s").read_bytes() == b"decoy"


def test_source_built_pyav_uses_patched_ffmpeg():
    assert av.__version__ == "18.1.0"
    assert av.library_versions["libavcodec"] == (63, 1, 102)
    assert not (Path(av.__file__).parent.parent / "av.libs").exists()
    version = subprocess.check_output(["ffmpeg", "-version"], text=True)
    assert version.startswith("ffmpeg version 9.0.2 ")
    assert "--disable-network" in version
    assert "--enable-version3" in version


def test_disabled_ffmpeg_components_are_absent():
    # CVE-2026-75142 and CVE-2026-75146 affect mpegenc and dashdec. The
    # RTSP redirect loop CVE-2026-107696 affects ff_rtsp_connect. None of
    # these network/input components may be exposed by our local-media build.
    muxers = subprocess.check_output(["ffmpeg", "-hide_banner", "-muxers"], text=True)
    demuxers = subprocess.check_output(
        ["ffmpeg", "-hide_banner", "-demuxers"], text=True
    )
    assert " mpeg " not in muxers
    assert " dash " not in demuxers
    for name in ("rtsp", "sdp", "satip"):
        assert f" {name} " not in demuxers
        assert name not in av.formats_available
    # Also exercise the input boundary: rejection must precede any connection.
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "rtsp", "-i", "rtsp://127.0.0.1/test"],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode != 0
    assert "Unknown input format: 'rtsp'" in result.stderr


def test_pipeline_imports_without_loading_weights():
    # These are the actual entry points used by h3.load_pipeline, not stubs.
    from diffusers import MiniMaxH3Transformer3DModel, ModularPipeline, TorchAoConfig
    from torchao.quantization import Int8WeightOnlyConfig
    from transformers import Qwen3VLForConditionalGeneration

    assert all(
        callable(value)
        for value in (
            MiniMaxH3Transformer3DModel,
            ModularPipeline,
            TorchAoConfig,
            Int8WeightOnlyConfig,
            Qwen3VLForConditionalGeneration,
        )
    )
    assert torch.version.cuda == "13.0"


def test_diffusers_real_video_audio_encoder(tmp_path):
    from diffusers.utils.export_utils import encode_video

    frames = [Image.new("RGB", (64, 64), (index * 2, 80, 120)) for index in range(124)]
    samples = torch.arange(32000 * 124 // 24, dtype=torch.float32) / 32000
    audio = (0.1 * torch.sin(2 * torch.pi * 440 * samples)).repeat(2, 1)
    output = tmp_path / "encoded.mp4"
    # Deterministic CPU pixels/tone exercise the exact production encoder. They
    # are media test inputs, not claimed model output or mock pipeline results.
    encode_video(
        frames, fps=24, output_path=str(output), audio=audio, audio_sample_rate=32000
    )
    with av.open(str(output)) as container:
        assert container.streams.video[0].codec_context.name == "h264"
        assert len(list(container.decode(video=0))) == 124
    with av.open(str(output)) as container:
        stream = container.streams.audio[0]
        assert stream.codec_context.name == "aac"
        assert stream.codec_context.sample_rate == 32000
        assert stream.codec_context.channels == 2
        assert (
            sum(frame.samples for frame in container.decode(audio=0)) >= audio.shape[1]
        )
    subprocess.run(
        ["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"],
        check=True,
    )
