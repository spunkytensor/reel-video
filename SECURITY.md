# Security Policy

## Supported versions

Until the project reaches 1.0, security fixes are provided for the latest release and the `main`
branch. Older pre-1.0 revisions may receive fixes at the maintainers' discretion.

## Reporting a vulnerability

Do not open a public issue containing vulnerability details, credentials, private prompts/media,
or other secrets. Use GitHub's private vulnerability reporting / Security Advisory flow for
[`spunkytensor/reel-video`](https://github.com/spunkytensor/reel-video/security/advisories/new). If it is
unavailable, open only a minimal public issue requesting a private channel.

Include the affected revision, reproduction steps, impact, and whether the issue involves local
files, generated media, CUDA/model execution, command execution, uploads, or dependencies. Remove
all secrets and private content.

## Deployment and trust boundary

The HTTP application is a **trusted-network, shared-access service**, not a multi-user security
boundary. `POST /session` automatically grants shared access. `Host` and `Origin` validation is
not authentication. Do not expose it to the Internet and do not use it to isolate mutually
untrusted users.

Security-sensitive areas include upload/path handling, SQLite state, media delivery, subprocesses,
FFmpeg/Pillow parsing, denial of service and resource exhaustion, accidental disclosure of inputs
or outputs, and dependency vulnerabilities. Never place secrets in repository files or issue
reports, and do not run untrusted code on a CUDA host with credentials, model caches, or private
media.

The MiniMax model and weights are third-party material governed separately; provider behavior,
output rights, model restrictions, and model-side data handling are not supplied by this project's
Apache license.
