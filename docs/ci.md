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

## GitHub Actions caches

pip downloads/wheels are cached using both CPU requirements and production
constraints; the existing npm download cache remains enabled. Installs and tests
still run every time, without restoring virtual environments or `node_modules`.

Both workflows use Buildx's GitHub Actions layer cache (`type=gha`, `mode=max`),
including intermediate FFmpeg/PyAV builds and runtime dependencies. Changed build
inputs invalidate affected layers. Images are loaded into Docker for the existing
runtime tests and scans, without a registry push. Separate `runtime-ci-amd64` and
`runtime-audit-amd64` scopes avoid competing writers across workflows.

Builds always check the base image with `pull: true`. Weekly and manual CVE audits
also use `no-cache: true` to refresh mutable package installations. Every audit
creates a new SBOM and runs the vulnerability scan and all security gates.

First runs are cold; `main` populates caches that subsequent PRs can read.
PR-written caches are restricted to their merge ref. Large CUDA layers incur
transfer/storage costs and may be evicted; cache misses trigger full builds.
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

`CVE Audit` runs on pull requests, `main` pushes, manual dispatch, and weekly.
It scans source dependencies and the runtime image using Syft 1.51.1 and Grype
0.118.0. The runtime audit applies `ci/runtime.openvex.json` and checks the CVE
baseline. Unresolved High/Critical findings and incomplete inventories fail.
SBOMs, reports, and runtime test results are retained for 14 days.

With Syft and Grype installed, run the runtime audit from the repository root:

```sh
docker build --pull -t reel-video:audit .
sh ci/runtime.sh reel-video:audit
syft scan docker:reel-video:audit --config ci/syft.yaml -o syft-json=sbom.syft.json
uv run --no-project --python 3.12 python ci/check_sbom.py sbom.syft.json runtime-image
grype sbom:sbom.syft.json --vex ci/runtime.openvex.json -o json --file grype.json --fail-on high
uv run --no-project --python 3.12 python ci/check_cve_baseline.py grype.json
```
