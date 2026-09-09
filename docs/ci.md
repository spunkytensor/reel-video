# CI and CVE audit

## What the CI badge means

The `CI` workflow runs on pull requests, pushes to `main`, and manual dispatch.
It runs Python 3.12 and Node 22 on an ordinary Ubuntu CPU runner.

- `ci/python.sh` is an explicit allowlist of real CPU contracts from the existing
  test suite: FastAPI validation/auth/CSRF, SQLite admission/idempotency/deletion,
  real Pillow decoding and uploads, and real FFmpeg encode/probe/decode of a
  deterministic test pattern. The pattern tests media processing, not AI generation.
- `ci/browser.cjs` starts the real production HTTP app with `start_worker=False`
  on loopback, using a disposable SQLite directory and ephemeral token. Chromium
  exercises session cookies, capability discovery, reference upload and removal,
  navigation/form preservation, real pending queue submission, and cancellation of
  the delete dialog followed by confirmed deletion. It checks desktop/mobile
  layouts, both themes, and uncaught JavaScript errors. It parses the actual inline
  frontend scripts for syntax errors. No HTTP response interception, fake worker,
  mocked inference, or fabricated completed video is used.
- Ruff checks all five production Python modules. JUnit results, browser captures,
  and HTTP server logs are uploaded under `cpu-ci-evidence` even when checks fail.
- `ci/runtime.sh` runs inside the actual runtime image without network or GPU
  access. It imports the real H3 pipeline classes, checks the CUDA wheel version
  and native FFmpeg/PyAV linkage, and exercises Diffusers' production audio/video
  encoder and the worker's FFmpeg finalizer with deterministic CPU media. This is
  a native compatibility check, not model execution.

Other legacy tests that patch pipelines, CUDA, downloads or response bodies are
**not part of this badge**. A pending queue request tests admission only. The worker
is deliberately disabled rather than substituted with a pretend implementation.
Neither CPU check reads operator credentials, uses existing media/state, nor
downloads model weights. Frontend dependencies are browser-native; the npm lockfile
contains only browser-test tooling.

## Run the same checks locally

Install Python 3.12, Node 22, and FFmpeg/ffprobe, then from the repository root:

```sh
python3.12 -m venv .venv-ci
. .venv-ci/bin/activate
python -m pip install -r ci/requirements.txt
npm ci
npx playwright-core install --with-deps chromium
sh ci/python.sh
npm test
docker build -t reel-video:ci .
sh ci/runtime.sh reel-video:ci
```

The CPU dependency subset is constrained by the production `requirements.txt`;
there is no separate floating Python version set for tests. `PYTHON` and
`CHROMIUM_PATH` optionally select local executables. Port 18088 must be free.
Test evidence is written to ignored `outputs/ci/`.

## Explicit CUDA boundary

Actual MiniMax H3 execution requires an NVIDIA CUDA GPU, compatible drivers,
model weights, and sufficient VRAM/host memory. Hosted CI **does not** verify:

- model loading, quantization or inference correctness;
- image conditioning or generated video/audio quality;
- real GPU memory budgets, OOM recovery, warm/cold workflow handoffs;
- in-flight GPU cancellation or GPU/network isolation during execution.

Those require deliberate hardware validation; see [measured verification](../VERIFICATION.md)
and the GPU verification scripts `verify_image_generation.py` and `verify_api.py`.
Do not treat old hardware results as verification of a new revision. Record the
tested revision, GPU/driver/CUDA versions, workload and measured outputs when running
hardware checks. No automatic self-hosted PR job is configured: untrusted PR code
must not run on an operator's GPU host with credentials or existing media.

## Why Syft and Grype, not Trivy

Syft creates the inventory; Grype matches that inventory against its vulnerability
database. This separates reproducible package evidence from advisory results that
change over time. The workflow pins Syft 1.51.1 and Grype 0.118.0 and pins their
GitHub actions to immutable revisions. Trivy is not installed or run.

`CVE Audit` runs on PRs, `main` pushes, manual dispatch, and weekly. Two independent
jobs inspect:

1. **Source:** declared Python requirements and the npm lockfile (including frontend
   test tooling, explicitly enabled with `javascript.include-dev-dependencies`). This
   is an inventory of declarations, not proof of installation.
2. **Runtime image:** a fresh `docker build --pull` of the actual Dockerfile. Syft
   catalogs installed Wolfi APK, Python and CUDA/NVIDIA wheel packages. Building and
   inspecting the image needs disk/network, but no GPU and no model weights. An
   image-build failure fails the audit rather than substituting a smaller CPU image.

`ci/check_sbom.py` rejects inventories that omit any locked Python package/version,
the source npm packages, or the runtime OS/FFmpeg inventory. A scan cannot pass just
because a cataloger silently skipped the dependencies we intended to audit.

Native Syft JSON preserves cataloger metadata for Grype. Both SBOM and Grype JSON
reports are retained for 14 days, including after threshold failures. Any High or
Critical finding not resolved by the exact-package assessment below fails, even
without a fix. Scanner/database errors also fail;
there are no blanket CVE ignores or `continue-on-error` gates.

`ci/runtime-cve-baseline.txt` records the 77 distinct CVEs originally reported as
High/Critical in the former Debian runtime. `ci/check_cve_baseline.py` additionally
fails if any unresolved finding still matches, **at any severity**. Changing distributions must
not turn an unresolved High into a passing Medium. This is a blocking regression
list, not an ignore list.

## Native runtime remediation

The base is a digest-pinned Wolfi **glibc** image, not Alpine/musl. It retains
Python 3.12 with Wolfi security backports (`3.12.14-r6`) and patched system Expat.
GCC 14 and Python/glibc headers remain installed for Torch/Triton runtime
compilation; GCC 14 is within CUDA 13's supported host-compiler range. These
choices preserve the intended CUDA wheel ABI, but do not replace GPU validation.

`docker/build-media.sh` builds checksum-pinned FFmpeg 9.0.1 and PyAV 18.1.0 from
source. PyAV links the same shared FFmpeg as the CLI instead of shipping the
older FFmpeg embedded in the PyPI wheel. The image includes only the local
H.264/AAC MP4 media paths used by this application, not FFmpeg's network inputs
or unrelated codecs. Exact sources, licenses and build instructions accompany
the installed media components; see [third-party notices](../THIRD_PARTY_NOTICES.md).
Both the SBOM and runtime tests reject an unexpected FFmpeg version/copy.
Installing `requirements.txt` alone outside Docker does not reproduce this native
build and is not covered by its runtime-image assessment.

### Three Python CPE false positives: evidence, not blanket ignores

The unassessed Grype scan still reports **three High matches** on
`pkg:apk/wolfi/python-3.12@3.12.14-r6?arch=x86_64&distro=wolfi-20230201`:
CVE-2026-3644, CVE-2026-4224 and CVE-2026-7210. Its generic NVD CPE ranges
`<3.13.13` / `<3.13.14` miss the fixes backported to Python 3.12.14. Do not
describe this as a raw zero-finding scan or infer that a lower severity fixes code.

The [immutable Wolfi recipe](https://github.com/wolfi-dev/os/blob/9fd668c6b384d7f44475cc21bc868495b635d23c/python-3.12.yaml)
identifies `3.12.14-r6`, pins the CPython source below, removes bundled Expat and
builds with `--with-system-expat`. The installed runtime uses Expat 2.8.4.
The relevant fixes in that exact upstream source are:

- **3644:** [cookie validation](https://github.com/python/cpython/blob/2abcf904b8dac8c999d2b3aac76681abb333798a/Lib/http/cookies.py)
  covers `Morsel.update`, `|=` and output validation of crafted/deserialized state.
- **4224:** [native DTD recursion accounting](https://github.com/python/cpython/blob/2abcf904b8dac8c999d2b3aac76681abb333798a/Modules/pyexpat.c)
  raises `RecursionError` instead of exhausting the C stack.
- **7210:** [pyexpat](https://github.com/python/cpython/blob/2abcf904b8dac8c999d2b3aac76681abb333798a/Modules/pyexpat.c)
  and [ElementTree](https://github.com/python/cpython/blob/2abcf904b8dac8c999d2b3aac76681abb333798a/Modules/_elementtree.c)
  use the [16-byte secret](https://github.com/python/cpython/blob/2abcf904b8dac8c999d2b3aac76681abb333798a/Include/pyhash.h)
  with `XML_SetHashSalt16Bytes` when compiled against Expat ≥2.8.0. Both the
  CPython change and the new Expat library are required; either alone is insufficient.

Before applying [`ci/runtime.openvex.json`](../ci/runtime.openvex.json), the audit
runs real stdlib regressions for cookie rejection and a 50,000-level DTD in the
built image. It also checks the Python APK revision, Expat version, and ELF
reference to `XML_SetHashSalt16Bytes` and system `libexpat.so.1`. These are
executable/binary checks, not patched functions or fabricated scan results.

The maintainer VEX assessment applies only to those three CVEs on that **exact
Wolfi package/version/architecture**, not other Python versions or packages.
Grype retains them under `ignoredMatches` with VEX reasons in the published JSON;
the assessment and runtime JUnit evidence are retained too. The baseline guard
rejects any suppression that does not match the reviewed VEX. Source audits do
not apply this VEX. Revalidate the source, binary evidence and assessment whenever
the package changes; remove obsolete assessments when advisory data is corrected.

Equivalent runtime commands with these tool versions installed:

```sh
docker build --pull -t reel-video:audit .
sh ci/runtime.sh reel-video:audit
syft scan docker:reel-video:audit --config ci/syft.yaml -o syft-json=sbom.syft.json
python3 ci/check_sbom.py sbom.syft.json runtime-image
grype sbom:sbom.syft.json --vex ci/runtime.openvex.json -o json --file grype.json --fail-on high
python3 ci/check_cve_baseline.py grype.json
```

Limits: a passing result means no unresolved matching advisory above the threshold
after the documented exact-package assessments, not absence of vulnerabilities.
Native libraries inside NVIDIA wheels can have incomplete package/advisory mapping.
The host NVIDIA driver is outside the container inventory. Transitive APK packages,
un-hashed Python wheels and the updated advisory database can change later results;
retain the SBOM and report for the actual run. These tools do not audit application
logic, model weights/licenses, secrets, or prove runtime exploitability. The image
scan is amd64-only. It is intentionally broader than a requirements-only check.

References: [Syft](https://github.com/anchore/syft),
[Grype](https://github.com/anchore/grype),
[Syft cataloger scope](https://oss.anchore.com/docs/guides/sbom/catalogers/).
