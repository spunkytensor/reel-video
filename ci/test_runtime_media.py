# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Run inside the actual image, offline and without GPU access or model weights."""

import subprocess
import sys
from http.cookies import CookieError, Morsel, SimpleCookie
from pathlib import Path
from xml.parsers import expat

import av
import pytest
import torch
from PIL import Image


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

    assert sys.version_info[:3] == (3, 12, 14)
    assert expat.version_info >= (2, 8, 0)
    symbols = subprocess.check_output(["readelf", "-Ws", pyexpat.__file__], text=True)
    assert "XML_SetHashSalt16Bytes" in symbols
    dependencies = subprocess.check_output(
        ["readelf", "-d", pyexpat.__file__], text=True
    )
    assert "libexpat.so.1" in dependencies
    # The VEX assessment applies to this APK revision only, not generic Python.
    subprocess.run(
        ["apk", "--no-network", "info", "--installed", "python-3.12=3.12.14-r6"],
        check=True,
    )


def test_source_built_pyav_uses_patched_ffmpeg():
    assert av.__version__ == "18.1.0"
    assert av.library_versions["libavcodec"] == (63, 1, 101)
    assert not (Path(av.__file__).parent.parent / "av.libs").exists()
    version = subprocess.check_output(["ffmpeg", "-version"], text=True)
    assert version.startswith("ffmpeg version 9.0.1 ")
    assert "--disable-network" in version
    assert "--enable-version3" in version


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
