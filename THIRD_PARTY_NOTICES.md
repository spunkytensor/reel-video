# Third-Party Notices

The repository's Apache License 2.0 applies to the original project code and documentation. It
does **not** relicense third-party material, including model weights or fonts.

## MiniMax H3 model and weights

MiniMax H3 and its weights are separate third-party material subject to the MiniMax community
license and restrictions at the pinned upstream revision:

<https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/42ed227ee7df40d41602854ae760620d6eb651fe/LICENSE>

Review and comply with those terms before downloading, using, or redistributing the model or
weights. They are not covered by this repository's Apache License 2.0.

## Fonts

The font binaries were copied from Reel Maestro and are served by the application,
not only its design mockups. Their upstream projects are
[Inter](https://github.com/rsms/inter) and
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono).

- Inter: Copyright (c) 2016 The Inter Project Authors, SIL Open Font License 1.1. Full text:
  [`assets/fonts/LICENSE-Inter.txt`](assets/fonts/LICENSE-Inter.txt).
- JetBrains Mono: Copyright 2020 The JetBrains Mono Project Authors, SIL Open Font License 1.1.
  Full text: [`assets/fonts/LICENSE-JetBrainsMono.txt`](assets/fonts/LICENSE-JetBrainsMono.txt).

## Native media components in the container

The runtime builds [FFmpeg 9.0.1](https://ffmpeg.org/) with GPL and version-3
support and links Wolfi's [x264](https://www.videolan.org/developers/x264.html)
(`2025.06.08-r7`, GPL-2.0-or-later). This FFmpeg build is GPL-3.0-or-later,
not Apache-2.0. [PyAV 18.1.0](https://github.com/PyAV-Org/PyAV) is BSD-3-Clause
and is built from source against those shared libraries, rather than using its
PyPI wheel's bundled FFmpeg. Preserve the applicable GPL obligations when
redistributing these linked media components.

The image includes the corresponding FFmpeg and PyAV source archives, x264
source at Wolfi's pinned revision and its immutable packaging recipe under
`/usr/share/reel-video/sources/`. The archives retain upstream notices and license
texts; FFmpeg's GPLv3 text is also copied alongside them. This project's media
build script and Dockerfile are under `/usr/share/reel-video/build/`. Source
archive checksums are checked during the build. The application license does not
override these licenses or those of the base OS/CUDA packages.

## Dependencies and distribution boundary

Python components are enumerated in `requirements.txt`, browser-test tooling in
`package-lock.json`, and system dependencies in `Dockerfile`. Their own licenses
continue to apply; an Apache header on this application does not relicense them.
The Qwen3-VL encoder is identified separately by the pinned H3 model license as
Apache-2.0; review the upstream notices when distributing model components.

Source does not bundle model weights. Startup downloads pinned components and
their upstream LICENSE/README into the local model volume. Before distributing
weights, generated outputs, or an image containing third-party dependencies, review
the applicable notices, geographic/use restrictions, downstream obligations, and
commercial terms. Runtime-image review must include FFmpeg/PyAV and NVIDIA/CUDA
packages. SBOM and CVE reports inventory components, but are not license clearance.
