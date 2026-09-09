# Copyright 2026 Spunky Tensor
# SPDX-License-Identifier: Apache-2.0

ARG BASE_IMAGE=cgr.dev/chainguard/wolfi-base@sha256:918a593b8268c222afd4e2c4f06860ac984e60719b4697e4c71d796bc8fcd042
FROM ${BASE_IMAGE} AS media-build

# Wolfi uses glibc, not musl, and backports fixes into its Python 3.12 packages.
ARG PYTHON_VERSION=3.12.14-r6
ARG X264_VERSION=2025.06.08-r7
RUN apk add --no-cache \
       python-3.12-dev=${PYTHON_VERSION} python-3.12=${PYTHON_VERSION} \
       libexpat1=2.8.4-r0 \
       py3.12-pip=26.2.1-r1 gcc-14-default=14.4.0-r1 glibc-dev=2.44-r5 \
       pkgconf=3.0.7-r0 nasm=3.02-r1 curl=8.22.0-r2 \
       ca-certificates=20260611-r1 xz=5.8.3-r3 make=4.4.1-r13 \
       x264-dev=${X264_VERSION} \
    && mkdir -p /etc/ld.so.conf.d \
    && echo /usr/local/lib > /etc/ld.so.conf.d/reel-video.conf

COPY docker/build-media.sh /build-media.sh
RUN sh /build-media.sh

FROM ${BASE_IMAGE} AS runtime
ARG PYTHON_VERSION=3.12.14-r6
ARG X264_VERSION=2025.06.08-r7

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/app/models/.cache/huggingface \
    PATH=/opt/venv/bin:$PATH

# Keep the C compiler: Torch/Triton may compile kernels at runtime. FFmpeg and
# PyAV build dependencies and the vulnerable distro media stack are not shipped.
# GCC 14 is within CUDA 13's supported host-compiler range (unversioned is not).
RUN apk add --no-cache \
       python-3.12=${PYTHON_VERSION} python-3.12-dev=${PYTHON_VERSION} \
       libexpat1=2.8.4-r0 \
       py3.12-pip=26.2.1-r1 x264-libs=${X264_VERSION} \
       ca-certificates=20260611-r1 gcc-14-default=14.4.0-r1 glibc-dev=2.44-r5 \
    && mkdir -p /etc/ld.so.conf.d \
    && echo /usr/local/lib > /etc/ld.so.conf.d/reel-video.conf

COPY --from=media-build /usr/local/ /usr/local/
COPY --from=media-build /sources/ /usr/share/reel-video/sources/
COPY --from=media-build /wheels/ /wheels/
COPY docker/build-media.sh Dockerfile /usr/share/reel-video/build/
RUN ldconfig && python3.12 -m venv /opt/venv \
    && python -m pip install --no-cache-dir pip==26.2.1 \
    && python -m pip install --no-cache-dir --no-deps /wheels/av-*.whl \
    && rm -rf /wheels

WORKDIR /app

COPY requirements.txt ./
# av==18.1.0 is already satisfied by our source-built wheel. Never replace it
# with PyPI's same-version wheel, which embeds the old FFmpeg libraries.
RUN python -m pip install --no-cache-dir --requirement requirements.txt

COPY h3.py image_codec.py provider.py server.py worker.py studio.html logo.png ./
COPY assets ./assets
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY docker-entrypoint.sh /usr/local/bin/reel-video-entrypoint

VOLUME ["/app/models", "/app/state"]
EXPOSE 8088

ENTRYPOINT ["reel-video-entrypoint"]
