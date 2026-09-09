# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

"""Strict, in-memory decoding and normalization of uploaded still images."""

from __future__ import annotations

import hashlib
import io
import warnings
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 8 * 2**20
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGE_DIMENSION = 8192
MAX_NORMALIZED_BYTES = 8 * 2**20

_FORMATS = {
    "image/jpeg": ("JPEG", lambda data: data.startswith(b"\xff\xd8\xff")),
    "image/png": ("PNG", lambda data: data.startswith(b"\x89PNG\r\n\x1a\n")),
    "image/webp": (
        "WEBP",
        lambda data: (
            len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
        ),
    ),
}


@dataclass(frozen=True)
class DecodedImage:
    data: bytes
    sha256: str
    width: int
    height: int


class _OutputTooLarge(Exception):
    pass


class _BoundedBytesIO(io.BytesIO):
    def write(self, data: bytes) -> int:
        end = self.tell() + len(data)
        if end > MAX_NORMALIZED_BYTES:
            raise _OutputTooLarge
        return super().write(data)


def _validate_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0:
        raise ValueError("invalid image dimensions")
    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        raise ValueError("image dimensions exceed limit")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("image pixel count exceeds limit")
    if width > height * 4 or height > width * 4:
        raise ValueError("image aspect ratio is outside limits")


def decode_image(data: bytes, content_type: str) -> DecodedImage:
    """Decode an uploaded still image into a metadata-free RGB PNG."""
    if not isinstance(data, bytes):
        raise ValueError("image data must be bytes")  # noqa: TRY004 - public contract
    if not data:
        raise ValueError("image is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image upload exceeds limit")

    mime = (
        content_type.split(";", 1)[0].strip().lower()
        if isinstance(content_type, str)
        else ""
    )
    expected = _FORMATS.get(mime)
    if expected is None:
        raise ValueError("unsupported image type")
    expected_format, has_magic = expected
    if not has_magic(data):
        raise ValueError("image type does not match content")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != expected_format:
                    raise ValueError("image type does not match content")
                _validate_dimensions(*image.size)
                if (
                    getattr(image, "is_animated", False)
                    or getattr(image, "n_frames", 1) != 1
                ):
                    raise ValueError("animated images are not supported")
                image.load()
                normalized = ImageOps.exif_transpose(image)
                _validate_dimensions(*normalized.size)

                if (
                    normalized.mode in ("RGBA", "LA")
                    or "transparency" in normalized.info
                ):
                    rgba = normalized.convert("RGBA")
                    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                    rgb = Image.alpha_composite(white, rgba).convert("RGB")
                else:
                    rgb = normalized.convert("RGB")

                # Pillow propagates source metadata through conversions unless removed.
                rgb.info.clear()
                output = _BoundedBytesIO()
                try:
                    rgb.save(output, format="PNG", optimize=False, compress_level=9)
                except _OutputTooLarge as exc:
                    raise ValueError("normalized image exceeds limit") from exc
                canonical = output.getvalue()
    except ValueError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValueError("image dimensions exceed safety limit") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError) as exc:
        raise ValueError("image is corrupt or truncated") from exc

    return DecodedImage(
        data=canonical,
        sha256=hashlib.sha256(canonical).hexdigest(),
        width=rgb.width,
        height=rgb.height,
    )
