# Installation

Production installation uses the generated digest-pinned TrueNAS release asset.
Follow `deploy/truenas/INSTALLATION.md`; it contains the exact release
authentication, dataset creation, UID/GID and ACL, secret, DNS, TLS, Custom App
UI, first-boot, backup/restore, upgrade, and rollback sequence. Each release
copies those operator files, the Windows SMB staging helper, and the auditable
one-shot initializer source beside its generated YAML. The no-shell initializer
model begins with a complete signed v0.1.0-rc.5 release asset set; do not
combine it with rc.3 assets or any other release. The signed server rc.4 tag
has no GitHub Release or YAML and is not an installation source.

## Production availability gate

A repository checkout is deliberately not deployable. Local Docker image IDs in
`docs/TESTING.md` are host-local build identities and must not be substituted
into production YAML. Install only when one actually published release provides
the manifest, four application-image digests, checksums and attestations,
compatible firmware identity, generated YAML, and operator bundle. The checked-
in `UNPUBLISHED_*` template must continue to fail at pull time.

For local development only:

```powershell
Copy-Item .env.example .env
.\scripts\create_local_secrets.ps1
docker compose -f compose.yaml -f compose.dev.yaml config --quiet
docker compose -f compose.yaml -f compose.dev.yaml up --build --wait
python -m pytest
npm --prefix frontend ci
npm --prefix frontend run check
```

Development credentials and simulated readings must stay in the development stack. They must never be copied into release artifacts or production. Production images are accepted only from the tagged GitHub workflow, by exact registry digest, with SBOM and provenance.

Required production facts:

- HTTPS origin defaults to `https://power-monitor.home.arpa:8443`.
- The certificate SAN contains the exact hostname and every browser/sensor trusts its CA.
- The only published container port is gateway TCP 8443.
- Application timezone defaults to `America/Los_Angeles`; storage and logs use UTC.
- Sensor readings accepted through `pm-protocol/1.0.0` are the sole usage authority.
- A bill PDF contributes rate facts only; it can create no History or usage
  record, and its original bytes/full OCR text are never persisted, even
  encrypted.
- The API host kernel exposes Landlock ABI 3 or newer and seccomp filter mode. API `/tmp` is a `nodev,noexec,nosuid` tmpfs; otherwise readiness and bill parsing fail closed.

For local development Compose only, force the parser-boundary self-test and
require exit zero:

```sh
docker compose exec api python -m backend.app.bill_rate_import.sandbox_check
```

On TrueNAS, use the signed release's browser/Windows health checks and the Apps
UI. Normal installation and verification do not require SSH, System Shell,
`docker exec`, or a second Compose project.

Do not deploy the repository template `deploy/truenas/power-monitor-v2.yaml`; explicit `UNPUBLISHED_*` sentinels make it non-deployable until the release workflow substitutes actual registry digests.
