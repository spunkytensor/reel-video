# Public-repository security baseline

Maintainer: Spunky Tensor (`spunkytensor`). Supported versions remain the latest
release and `main`, as defined in [SECURITY.md](../SECURITY.md). The project and
contribution license remains Apache-2.0, with no separate CLA or DCO requirement.

The adopted [shared baseline](https://github.com/spunkytensor/.github/blob/ed53814ed23f76c11fa4a91f57f99de903c18bfc/docs/baseline.md)
is pinned at that revision. Adoption is partial; automation is not legal clearance.

## Automated coverage

- `Spunky Tensor security` calls the pinned reusable Trivy source scanner on PRs,
  `main`, manual dispatch and nightly at **09:19 UTC** (01:19 PST / 02:19 PDT).
  Its source scan includes npm development dependencies and declared Python
  dependencies, but does not install/resolve all Python transitive dependencies.
  High/Critical findings, including unfixed findings, and empty inventories fail.
  Trivy has no suppression or VEX input in this baseline.
- Source and actual locally built Linux amd64 runtime inventories remain checked
  by Syft/Grype, including Python, OS, CUDA wheels, native FFmpeg/PyAV linkage,
  inventory completeness and the severity-independent CVE baseline. Native CPU
  regressions run before runtime OpenVEX applicability statements are applied.
  No scanner or existing VEX statement has been removed or translated.
- Public-repository CodeQL supplements the baseline for Python, JavaScript and
  Actions; it is skipped for private repositories and is not baseline-required.
  Full-inventory Trivy replaces dependency review. Existing Dependabot configuration
  covers pip, npm, Docker and Actions. Action references use full commit pins.
- [Shared source evidence](https://github.com/spunkytensor/reel-video/actions/workflows/public-repo-security.yml)
  includes SPDX/CycloneDX SBOMs, all-severity vulnerability reports, tool/database
  metadata, source identity and checksums (30 days).
  [Native audit evidence](https://github.com/spunkytensor/reel-video/actions/workflows/security.yml)
  includes Syft inventories converted to SPDX/CycloneDX with checksums, Grype JSON,
  VEX and runtime test results (14 days).

## Distribution and attribution

The repository ships source and bundled fonts; `run.sh` builds the Docker runtime
locally. There are no published GitHub releases or image-publishing workflows at
adoption. No supported released-image digest is registered for nightly scanning.
Rebuilding `main` is not coverage of an older image a user still runs. Register
each supported public image's platform manifest digest with the shared workflow
before distributing releases; include every supported architecture.

[Third-party notices](../THIRD_PARTY_NOTICES.md) and the project license are copied
into the runtime. Font license texts ship in `assets/fonts`. The runtime includes
FFmpeg/PyAV/x264 sources, applicable license texts, pinned packaging recipe and
build files under `/usr/share/reel-video`; these are not replaced by a generic
dependency list. NVIDIA/CUDA redistribution, GPL source-delivery obligations and
model-specific restrictions require review before redistribution. Model weights
are downloaded separately at startup and are not covered by the source scan or
the project's Apache license. Review other assets' provenance before release.

## Remaining maintainer and administrator work

- Verify private vulnerability reporting and the existing fallback contact route;
  enable/verify dependency graph, alerts/security updates, secret scanning and
  push protection. API visibility did not establish these settings at adoption.
  This change does not alter GitHub settings.
- Configure branch protection, required checks and workflow/policy code ownership
  after observing real check names. Confirm reviewers, 2FA and periodic access
  review. Public CodeQL duplicate default setup may need admin review.
- Assign vulnerability findings an owner and remediation date. Existing OpenVEX
  has scoped package/version and regression evidence but needs named reviewer,
  expiry/review date and tracking references; do not invent legal approval or
  silently broaden its applicability. Baseline exceptions require all of these.
- Reconcile complete resolved runtime/build inventories with assets and native
  build outputs. Pilot attribution tooling (ORT/ScanCode) and review unknown or
  conflicting licenses. Produce reviewed `THIRD_PARTY_NOTICES.txt` plus required
  texts for release artifacts without discarding the current project notices.
- Before releases, publish checksums, both SBOM formats, reviewed notices and
  provenance tied to immutable artifact digests as durable release assets. Actions
  artifacts expire and are not release attestations or a SLSA claim.
- Verify the first successful scheduled run after merge and connect organization
  freshness reporting to `.github/workflows/public-repo-security.yml`; flag scans
  older than 36 hours. Cron delivery is best effort and inactive public-repository
  schedules can disable. No successful nightly run is claimed before activation.

No model download, GPU execution, GitHub settings change, release publication or
license change is part of this adoption.
