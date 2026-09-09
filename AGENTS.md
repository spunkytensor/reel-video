# AGENTS.md

Guidance for coding agents working on Reel Video.

## Constraints

- Use uv with Python 3.12 and Node.js 22; the runtime Docker image includes CUDA dependencies.
- Keep changes focused. Never commit or print secrets, weights, private inputs, generated media,
  local state, virtual environments, dependency directories, or test output.
- Do not download models or run GPU generation unless explicitly requested. CI intentionally uses
  the real app with its worker disabled and does not claim inference coverage.
- Treat the app as a trusted-network shared service. `POST /session` grants shared access;
  `Host`/`Origin` are not authentication. Do not claim Internet safety or user isolation.
- Preserve attribution and license notices for third-party code, fonts, models, and assets.

## Checks

Follow [`docs/ci.md`](docs/ci.md). The focused CPU checks are:

```sh
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt sh ci/python.sh
uv run --no-project --python 3.12 --with-requirements ci/requirements.txt npm test
```

They require the documented Python/Node dependencies, Chromium, and FFmpeg. CUDA validation is
intentional and separate; record hardware, driver, CUDA, revision, workload, and measured output.

## Source map

- `server.py` — FastAPI HTTP/session API and persistence.
- `worker.py` — queued generation worker.
- `h3.py`, `provider.py`, `image_codec.py` — model pipeline and image handling.
- `studio.html`, `assets/` — browser UI and static assets.
- `ci/`, `docs/ci.md` — CPU checks and supply-chain audit documentation.

When behavior, security boundaries, dependencies, or checks change, update their tests and
relevant documentation. Do not weaken checks or replace real CPU/media behavior with mocks merely
to make CI pass.

## Original source headers and comments

All original code, tests, verification scripts, CSS/HTML, Dockerfiles and YAML automation
must carry `Copyright 2026 Spunky Tensor` and `SPDX-License-Identifier: Apache-2.0`
in the first eight lines, using the language's comment syntax. Preserve shebangs and
HTML doctypes. Run `uv run --no-project --python 3.12 python ci/check_headers.py`;
JSON uses license metadata instead of comments. Do not relicense third-party fonts
or remove attribution.

Explain non-obvious contracts, concurrency, memory and security tradeoffs with rationale
comments; avoid narrating obvious statements. Keep documentation factual and task-focused;
omit project history and design justifications.
