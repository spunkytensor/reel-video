#!/bin/sh
# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

set -eu
mkdir -p /sources /wheels /build
curl -fsSL https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz -o /sources/ffmpeg-9.0.1.tar.xz
echo 'cf38e0e28c7e5605942c4a77755349b0145804a397af37eb1fb4c77cb237f635  /sources/ffmpeg-9.0.1.tar.xz' | sha256sum -c -
curl -fsSL https://files.pythonhosted.org/packages/8d/f4/f22114d30d3435e38c6af2b4870f37b864403dca6ae7af747a289ce0a18e/av-18.1.0.tar.gz -o /sources/av-18.1.0.tar.gz
echo '47bfc286e1bc9de7ab4681fc2b575cd2460a66919d31ffe1bd5aa54fae531a28  /sources/av-18.1.0.tar.gz' | sha256sum -c -

# Corresponding source and immutable packaging recipe for Wolfi's x264 library.
# The upstream GitLab archive endpoint challenges non-browser build clients.
curl -fsSL https://codeload.github.com/mirror/x264/tar.gz/b35605ace3ddf7c1a5d67a2eb553f034aef41d55 -o /sources/x264.tar.gz
echo 'cd71a7515b0e9a012e1ac9b1f8415bebcaf6fc97d4db32286642ac4c0fbe24f9  /sources/x264.tar.gz' | sha256sum -c -
curl -fsSL https://raw.githubusercontent.com/wolfi-dev/os/8339db0c8c9bb1f53116c80e08309a2b96d82bc9/x264.yaml -o /sources/x264-wolfi.yaml

tar -xf /sources/ffmpeg-9.0.1.tar.xz -C /build
cd /build/ffmpeg-9.0.1
# Only local H.264/AAC MP4 and the real CPU test-pattern sources are needed.
# Removing unused network protocols/decoders reduces the input attack surface;
# this is not a scanner exclusion. Keep GPL notices and corresponding sources.
./configure --prefix=/usr/local --enable-shared --disable-static \
    --disable-doc --disable-debug --disable-autodetect --disable-network \
    --enable-gpl --enable-version3 --enable-libx264 --disable-everything \
    --enable-protocol=file,pipe --enable-demuxer=mov \
    --enable-muxer=mp4,null --enable-decoder=h264,aac,wrapped_avframe,pcm_s16le \
    --enable-encoder=libx264,aac,wrapped_avframe,pcm_s16le --enable-parser=h264,aac \
    --enable-filter=scale,pad,setsar,aresample,aformat,format,abuffer,abuffersink,buffer,buffersink \
    --enable-indev=lavfi --enable-filter=testsrc2,sine
make -j2
make install
ldconfig
cp COPYING.GPLv3 /sources/FFmpeg-COPYING.GPLv3

python3.12 -m venv /build/venv
/build/venv/bin/python -m pip install --no-cache-dir \
    pip==26.2.1 setuptools==84.0.0 cython==3.3.0 wheel==0.48.0
# A source build is essential: PyAV wheels bundle a different FFmpeg ABI.
PKG_CONFIG_PATH=/usr/local/lib/pkgconfig /build/venv/bin/python -m pip wheel \
    --no-cache-dir --no-build-isolation --no-deps \
    --wheel-dir /wheels /sources/av-18.1.0.tar.gz
