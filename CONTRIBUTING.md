# Contributing to Reel Video

Thanks for helping improve the project. Keep pull requests focused, and open an issue before a
large behavior or architecture change.

## Before you start

- Use uv with Python 3.12 and Node.js 22. Docker is needed for runtime-image checks.
- Do not commit secrets, model weights, generated media, local databases, virtual environments,
  dependency directories, or build/test output.
- Identify third-party code, assets, documentation, models, and fonts in the pull request and add
  all required license and attribution material. Do not assume the repository's Apache license
  covers them.
- Do not run model downloads or GPU generation unless you intentionally choose to do so on an
  appropriate CUDA host. Never run untrusted pull-request code on a GPU host containing
  credentials, weights, existing state, or media.

## Setup and checks

Install uv, Node 22, FFmpeg/ffprobe, and Chromium as described in
[`docs/ci.md`](docs/ci.md). Run the CPU checks from the repository root:

```sh
uv python install 3.12
npm ci
npx playwright-core install --with-deps chromium
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt sh ci/python.sh
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt npm test
```

Runtime-image and audit commands are in [`docs/ci.md`](docs/ci.md).

Actual MiniMax H3 generation requires compatible NVIDIA CUDA hardware and substantial resources.
Only run `verify_image_generation.py` or `verify_api.py` deliberately, record the revision and
hardware/software environment, and keep credentials, inputs, weights, and outputs private unless
you have confirmed they are safe and licensed to share.

## Service boundary

This is a trusted-network, shared-access service. `POST /session` automatically grants shared
access; `Host` and `Origin` checks are request-integrity controls, not authentication. Do not
present the service as suitable for Internet exposure or user isolation. Changes that alter this
boundary must update tests and security documentation.

## Pull request expectations

Put `Copyright 2026 Spunky Tensor` and `SPDX-License-Identifier: Apache-2.0` in
format-appropriate comments at the top of all original source, tests and automation.
Preserve shebangs/doctypes and upstream notices. Run
`uv run --no-project --python 3.12 python ci/check_headers.py`;
JSON uses license metadata and third-party font notices keep their own terms.
Explain non-obvious invariants and security/memory tradeoffs for human maintainers,
rather than commenting every statement. Format Python changes with Ruff.

Explain the change and its user impact, list checks actually run, and disclose network access,
GPU/model execution, new dependencies, and third-party material.

## License

Unless you explicitly state otherwise in writing, contributions intentionally submitted for
inclusion are provided under the same [Apache License 2.0](LICENSE), as described by section 5 of
that license. There is no separate CLA or DCO requirement.
