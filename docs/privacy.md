# Data handling and operations

Reel Video is a shared trusted-network application, not a hosted multi-tenant service.
Every reachable client can obtain a Studio session, view inputs/history/media and
manage shared jobs. Host/Origin checks do not identify people. Use a trusted LAN/VPN
and firewall; the default listener publishes plain HTTP on all host interfaces.

## Stored data and deletion

- The state volume holds SQLite job requests (including prompts), upload images,
  session digests, idempotency records and generated media. There is no application
  encryption at rest. Protect the volume and backups with host access controls.
- Studio stores uncertain submission keys and payloads, including prompts, in tab
  `sessionStorage` before sending them. This permits reconciliation after a lost
  response; clearing it prematurely loses that recovery information. Appearance
  settings are stored in `localStorage`; the API key is not stored there.
- Video deletion hides the job from the library and removes its media, but retains
  job metadata and idempotency tombstones. Authorized polling can still expose the
  retained request. Deletion is not removal of all prompts or secure erasure.
- Completed media expires after three days by default. Upload expiry and active-job
  pinning are enforced by the store; images required by active work are retained.
  The worker performs physical cleanup, so a stopped worker delays reclamation.
  Linux downloads already holding open file descriptors can finish after unlink.
- Logs, SQLite free pages, browser state and backups can retain information after
  normal deletion. Do not submit secrets as prompts.

## Credentials and networking

Both launch scripts source `.env` as trusted shell code. Use `chmod 600 .env` and
never source a template from an untrusted contributor. Docker administrators can
inspect container environment credentials; host access is outside the API boundary.

Startup contacts Hugging Face to download pinned components into the model volume.
Generation uses local weights and the worker creates Linux network namespaces before
loading the model. Failure to establish that isolation stops the worker. The launcher
uses `seccomp=unconfined` to permit this; it weakens container syscall isolation and
is not suitable for hostile tenants.

## Backup, restore and diagnosis

Stop the service with `./stop.sh` before taking a consistent snapshot of the entire
state volume, including SQLite/WAL files and media. Protect the backup like the live
data. Restore the complete snapshot to an isolated deployment of the same code
revision first; do not merge database files from different runs or restore over a
running worker. A restored snapshot also restores its retained prompts and sessions.
Keep model weights separately, subject to the model's redistribution terms.

Stopping/rebuilding a container does not delete its named volumes. Removing a state
volume destroys jobs, uploads and media; removing a model volume forces downloads.
These are deliberate operator actions, not part of routine startup or upgrades.

An HTTP response alone does not establish model readiness. Inspect the authenticated
`/health` worker state and sanitized container logs. If loading fails, check available
RAM/VRAM, driver/toolkit compatibility, model-download disk space and namespace support.
Do not stop unrelated GPU jobs or disable isolation merely to make a health check pass.
