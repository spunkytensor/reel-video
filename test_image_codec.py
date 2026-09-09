# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

import hashlib
import io

import pytest
from PIL import Image, PngImagePlugin

import image_codec
from image_codec import DecodedImage, decode_image


def encoded(format="PNG", mode="RGB", size=(8, 4), color=(10, 20, 30), **save):
    stream = io.BytesIO()
    Image.new(mode, size, color).save(stream, format=format, **save)
    return stream.getvalue()


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")],
)
def test_decodes_supported_formats_to_canonical_rgb_png(format, mime):
    result = decode_image(encoded(format), mime + "; charset=binary")
    assert isinstance(result, DecodedImage)
    assert result.width == 8 and result.height == 4
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n")
    assert result.sha256 == hashlib.sha256(result.data).hexdigest()
    with Image.open(io.BytesIO(result.data)) as output:
        assert output.mode == "RGB"
        assert output.info == {}


def test_strips_png_text_and_color_metadata():
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret")
    source = encoded(pnginfo=info, icc_profile=b"not-a-real-profile")
    result = decode_image(source, "image/png")
    with Image.open(io.BytesIO(result.data)) as output:
        assert "Comment" not in output.info
        assert "icc_profile" not in output.info


def test_applies_exif_orientation():
    image = Image.new("RGB", (2, 4), "red")
    exif = Image.Exif()
    exif[274] = 6
    stream = io.BytesIO()
    image.save(stream, "JPEG", exif=exif)
    result = decode_image(stream.getvalue(), "image/jpeg")
    assert (result.width, result.height) == (4, 2)


def test_composites_transparency_onto_white():
    image = Image.new("RGBA", (2, 1))
    image.putdata([(255, 0, 0, 0), (0, 0, 0, 128)])
    stream = io.BytesIO()
    image.save(stream, "PNG")
    result = decode_image(stream.getvalue(), "image/png")
    with Image.open(io.BytesIO(result.data)) as output:
        assert output.getpixel((0, 0)) == (255, 255, 255)
        assert output.getpixel((1, 0)) == (127, 127, 127)


@pytest.mark.parametrize("data", [b"", b"not an image", b"\x89PNG\r\n\x1a\n"])
def test_rejects_empty_fake_and_truncated_data(data):
    with pytest.raises(ValueError):
        decode_image(data, "image/png")


def test_rejects_wrong_mime_and_unsupported_mime():
    png = encoded()
    with pytest.raises(ValueError, match="does not match"):
        decode_image(png, "image/jpeg")
    with pytest.raises(ValueError, match="unsupported"):
        decode_image(png, "image/gif")


def test_rejects_animated_webp_and_png():
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    for format, mime in (("WEBP", "image/webp"), ("PNG", "image/png")):
        stream = io.BytesIO()
        frames[0].save(stream, format, save_all=True, append_images=frames[1:])
        with pytest.raises(ValueError, match="animated"):
            decode_image(stream.getvalue(), mime)


def test_rejects_animated_gif_as_unsupported():
    frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
    stream = io.BytesIO()
    frames[0].save(stream, "GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(ValueError, match="unsupported"):
        decode_image(stream.getvalue(), "image/gif")


def test_rejects_upload_dimension_pixel_aspect_and_output_limits(monkeypatch):
    png = encoded(size=(8, 4))
    monkeypatch.setattr(image_codec, "MAX_UPLOAD_BYTES", len(png) - 1)
    with pytest.raises(ValueError, match="upload"):
        decode_image(png, "image/png")
    monkeypatch.setattr(image_codec, "MAX_UPLOAD_BYTES", 10_000)
    monkeypatch.setattr(image_codec, "MAX_IMAGE_DIMENSION", 7)
    with pytest.raises(ValueError, match="dimensions"):
        decode_image(png, "image/png")
    monkeypatch.setattr(image_codec, "MAX_IMAGE_DIMENSION", 100)
    monkeypatch.setattr(image_codec, "MAX_IMAGE_PIXELS", 31)
    with pytest.raises(ValueError, match="pixel"):
        decode_image(png, "image/png")
    monkeypatch.setattr(image_codec, "MAX_IMAGE_PIXELS", 1_000)
    with pytest.raises(ValueError, match="aspect"):
        decode_image(encoded(size=(9, 2)), "image/png")
    monkeypatch.setattr(image_codec, "MAX_NORMALIZED_BYTES", 16)
    with pytest.raises(ValueError, match="normalized"):
        decode_image(png, "image/png")


def test_rejects_decompression_bomb_warning(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    with pytest.raises(ValueError, match="safety"):
        decode_image(encoded(size=(2, 2)), "image/png")
