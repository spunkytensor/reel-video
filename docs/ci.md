# CI and CVE audit

## Checks

The `CI` workflow runs on pull requests, pushes to `main`, and manual dispatch.
It uses Python 3.12 and Node 22 on an Ubuntu CPU runner.

- `ci/python.sh`: source headers, Ruff, API validation/authentication, SQLite,
  image uploads/decoding, and FFmpeg encoding/decoding.
- `ci/browser.cjs`: Chromium tests against the HTTP server with temporary state
  and the worker disabled; desktop/mobile layouts, themes, uploads, and job deletion.
- `ci/runtime.sh`: pipeline imports, native FFmpeg/PyAV linkage, Python security
  regressions, and media encoding inside the runtime image without network or GPU access.

Results, screenshots, and logs are uploaded as `cpu-ci-evidence`.

## Run the same checks locally

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Node 22,
FFmpeg/ffprobe, and Docker. From the repository root:

```sh
uv python install 3.12
npm ci
npx playwright-core install --with-deps chromium
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt sh ci/python.sh
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt npm test
docker build -t reel-video:ci .
sh ci/runtime.sh reel-video:ci
```

uv installs the CPU test dependencies from `ci/requirements.txt`, constrained by
`requirements.txt`. Port 18088 must be free. `PYTHON` and `CHROMIUM_PATH` can
select alternative executables. Local results are written to `outputs/ci/`.

Change dependencies in `requirements.in`, then regenerate `requirements.txt` with
the pip-compile command in its header. Dependabot reuses that command.
`--strip-extras` keeps the file valid as a pip constraints file, and
`--allow-unsafe` keeps the `setuptools` security pin.

## GitHub Actions caches

pip downloads/wheels are cached using both CPU requirements and production
constraints; the existing npm download cache remains enabled. Installs and tests
still run every time, without restoring virtual environments or `node_modules`.

Runtime images are always built from scratch, without a Docker layer cache.
Uploading and restoring the multi-GB CUDA layers through the GitHub Actions cache
took longer than rebuilding them. Every build therefore refreshes mutable package
installations, and `pull: true` checks the base image. Images are loaded into
Docker for the runtime tests and scans, without a registry push.

Both CVE audit jobs restore the Grype vulnerability database cached for the
current UTC date. Grype still checks for a newer database and downloads it when
one exists. Every audit creates a new SBOM and runs the vulnerability scan and
all security gates.

No test results, credentials, model weights, media or application state are cached.

September 9 baseline: [CI](https://github.com/spunkytensor/reel-video/actions/runs/34316958696)
took 9m04s, including 7m16s in runtime build/test and 43s installing host dependencies.
The [runtime audit](https://github.com/spunkytensor/reel-video/actions/runs/34316958695)
took 9m17s, including 6m14s building the image. Compare cold/warm build and cache
transfer times before claiming a measured speedup.

## GPU verification

Actual MiniMax H3 execution requires an NVIDIA CUDA GPU, compatible drivers,
model weights, and sufficient VRAM/host memory. Hosted CI does not run model
loading, inference, quality, memory-budget, or GPU-cancellation tests.

Run `verify_image_generation.py` or `verify_api.py` only on a supported GPU host.
Record the revision, GPU, driver/CUDA versions, workload, and results.

## CVE audit

`CVE Audit` runs on pull requests, `main` pushes, manual dispatch, and nightly
at 09:19 UTC.
It scans source dependencies and the runtime image using Syft 1.51.1 and Grype
0.118.0. The runtime audit applies `ci/runtime.openvex.json` and checks the CVE
baseline. Unresolved High/Critical findings and incomplete inventories fail.
Syft inventories and their SPDX/CycloneDX conversions with checksums, reports,
and runtime test results are retained for 14 days.

The runtime uses Wolfi Python `3.12.16-r3`, Expat `2.9.0-r0`, OpenSSL `3.6.5-r2`, zlib
`1.3.2.1_rc20260601-r0`, Brotli `1.2.1-r1`, and source-built FFmpeg `9.0.2`.
Python 3.12.16 includes the compromised-certificate key denylist for
`CVE-2026-115557`. Both stages explicitly install OpenSSL 4 `4.0.3-r6`
and Expat 2.9: the pinned base's OpenSSL 4.0.2 breaks SHA-224, while older
Expat lacks the new Python binary's `XML_GetCurrentByteIndex64` symbol.
Runtime checks exercise SHA-224, fresh offline venv creation, and the compiled
denylist entry. Wolfi pip and its venv seed wheel are pinned to `26.2.2-r2`,
including vendored msgpack `1.2.3` (`CVE-2026-114613`) and certifi `2026.10.23`.
Neither stage replaces this patched pip with PyPI's wheel. Setuptools is pinned
to `84.1.0` in production and the PyAV build environment for `GHSA-xmj3-9gf7-h52w`.
Both Docker stages explicitly upgrade all three Brotli libraries inherited from
the pinned base image to fix `CVE-2026-111497`. The Python lock pins `filelock`
`4.2.1` to fix `CVE-2026-112940` (SQLite-backed lock file overwrite) and its
two Low-severity Windows advisories. These are package upgrades, not VEX exemptions.

Native runtime tests exercise a crafted `CVE-2026-82049` tarfile archive against the stdlib fix and
verify the FFmpeg MPEG muxer and DASH, RTSP, SDP and SAT>IP demuxers are absent
before applying the three exact-package VEX assessments. The RTSP input check
also rejects a URL as an unknown format, covering `CVE-2026-107696` without
connecting to a server. Upstream FFmpeg 9.0.2 is affected by that redirect loop;
the vulnerable code is excluded from this local-media build, not patched.
Those assessments apply only to this tested image
build, not to unmodified packages or other FFmpeg builds with the same version.
The original CVE baseline and High/Critical failure threshold remain enforced.

The source scan and Dependabot still report Medium `CVE-2026-69112` (`GHSA-4j2p-28q2-5m79`)
in accelerate `1.14.0` without a published fixed version as of 2026-11-01.
Dependabot attributes it to `ci/requirements.txt` through its `-c ../requirements.txt`
constraint; the CI environment does not install accelerate, but the runtime image does.
The advisory range ends at `1.14.0`, but accelerate `1.15.0` keeps the same
`weight_map` handling, so an upgrade alone is not remediation. The affected
`load_checkpoint_in_model` and `load_checkpoint_and_dispatch` functions have no
callers in this repository, diffusers `0.40.0` or transformers `5.21.0`.
`h3.py` loads only the pinned model revision with `local_files_only=True`.
The finding is not suppressed; the existing High/Critical gate is unchanged.

`Spunky Tensor security` in `.github/workflows/public-repo-security.yml` runs on
the same events and nightly schedule. It calls the shared Trivy 0.76.1 workflow pinned
to `2a2d755bd3279917e687d9109489bebadeab4c50`, retaining source SPDX and CycloneDX
SBOMs, all-severity vulnerability JSON, tool/database metadata, source identity,
and checksums for 30 days. Empty inventories, scanner errors, and High/Critical
findings (including unfixed findings) fail. It does not apply the runtime VEX.
Full-inventory Trivy replaces dependency review. Public-only CodeQL analyzes Python,
JavaScript and Actions as supplemental coverage; it is not required for private repositories.

See [baseline coverage and remaining decisions](security-baseline.md). Workflow
artifacts are temporary evidence, not durable release SBOM downloads.

With Syft and Grype installed, run the runtime audit from the repository root:

```sh
docker build --pull -t reel-video:audit .
sh ci/runtime.sh reel-video:audit
syft scan docker:reel-video:audit --config ci/syft.yaml -o syft-json=sbom.syft.json
uv run --no-project --python 3.12 python ci/check_sbom.py sbom.syft.json runtime-image
grype sbom:sbom.syft.json --vex ci/runtime.openvex.json -o json --file grype.json --fail-on high
uv run --no-project --python 3.12 python ci/check_cve_baseline.py grype.json
```
