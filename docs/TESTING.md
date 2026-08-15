# Testing and evidence

Local server gates from repository root:

```powershell
python -m pip install -e '.[dev]'
python -m ruff check backend worker tests scripts
python -m mypy backend worker
python -m pytest --junitxml=release-test-results.xml
npm --prefix frontend ci
npm --prefix frontend run check
python scripts/validate_release.py
```

When Docker is available, validate the backup image and a temporary rendered
Compose configuration. The checked-in production template passes YAML/Compose
structure validation but contains deliberately invalid `UNPUBLISHED_*` image
references; it therefore fails closed at pull/deployment. Release CI renders
actual registry digests after push.

```powershell
docker build --file backup/Dockerfile --tag pm-backup:test .
python scripts/render_truenas_release.py --template deploy/truenas/power-monitor-v2.yaml `
  --output release/power-monitor-v2-test.yaml --manifest release/release-manifest-test.json `
  --version 0.1.0-rc.3 --revision 0123456789abcdef0123456789abcdef01234567 `
  --api-digest sha256:<published-api-digest> `
  --frontend-digest sha256:<published-frontend-digest> `
  --gateway-digest sha256:<published-gateway-digest> `
  --backup-digest sha256:<published-backup-digest>
docker compose -f release/power-monitor-v2-test.yaml config --quiet
python scripts/verify_release_artifacts.py --manifest release/release-manifest-test.json
```

Release workflow gates cover backend unit/integration and PostgreSQL migration
tests; frontend checks; shared protocol vectors; Compose/hardening checks;
dependency/secret/CodeQL/container scans; SBOM/provenance; a clean digest-pinned
deployment with TLS, SSE, upload limits, restarts, encrypted backup, and actual
isolated restore; and firmware release compatibility. The migration gate uses
authenticated GitHub release metadata to select the latest same-major,
non-draft public V2 Release, requires that exact tag in the checkout, and then
exercises only the forward path from that release's database to the current
release. It fails closed instead of treating a newer unpublished or failed tag
as an installable predecessor. It does not run prior binaries against the
post-upgrade database or prove rollback compatibility. The GitHub-hosted
clean-deployment smoke does not exercise application rollback and records
`not_exercised_github_hosted_smoke`, never a rollback pass. Target upgrade and
rollback evidence remains separate.

The API image starts Uvicorn on Python's standard `asyncio` loop because the
fail-closed PDF sandbox establishes its boundary through asyncio subprocess
pipes; the restricted-image gate validates that exact path. Successful sandbox
evidence is cached for five minutes, while a failed probe is retried after at
most five seconds. API healthcheck client and container timeouts both exceed the
sandbox's bounded self-test duration. If the digest-pinned deployment smoke
fails, it uploads redacted Compose status, container health state, and service
log-event JSONL (`deployment-test-report-failure-log-events.jsonl`) before
cleanup. The event stream is bounded and contains only known service, UTC
timestamp, and event enums—never raw log text. Those diagnostics never make a
failed smoke eligible for release assembly.

The target TrueNAS deployment suite records machine-readable evidence for clean
migration, service health, HTTPS routing and headers, SSE proxying, upload
rejection, dataset permissions, backup/restore, every individual service
restart, full-stack restart, and persistence. A rollback result additionally
requires an explicit release-specific test that restores or clones the matching
pre-upgrade database into a validated recovery target before binding prior
digests. Target-TrueNAS execution remains separate physical deployment evidence
and cannot be inferred from a GitHub-hosted Docker runner.

Firmware host/fault/simulation/HIL tests live in the independent firmware repository. Hardware certification requires actual marked ESP32-S3/PZEM/SD evidence for PZEM readings, endurance sample, AP/server/DNS/TLS outages, physical cycles, USB recovery, OTA/rollback, and 72-hour soak. A passing simulator cannot set hardware status to passed.

Every evidence report records schema, version, full revision, UTC generation time, outcome, exact command/environment, input/output checksums, test counts, failures/skips, and physical/simulated classification. A release gate must not be reported passed from missing, stale, or unparsable evidence.

## Local validation snapshot

On 2026-08-13/14, before publication, the implementation was exercised locally.
This snapshot is development evidence. It is not a signed tag or GitHub release,
not a registry digest, not a target-TrueNAS result, and not physical hardware
certification.

### Server, contracts, and database

- The portable Python suite collected 105 tests: **101 passed and 4 expected
  skips**. The skips were two Linux-only PDF-sandbox integration tests, one
  PostgreSQL-only lease test, and the opt-in live-API test. JUnit evidence is
  `.test-runtime/full-test-results.xml`.
- The role-separated PostgreSQL 17.10 suite used the real role initializer and
  Alembic chain under distinct migrator/API/worker/backup/restore identities:
  **102 passed and 3 expected skips**. JUnit evidence is
  `.test-runtime/ci-role-postgres-tests.xml`.
- A fresh PostgreSQL 17.10 database migrated to head `20260813_0007`, downgraded
  to base, and migrated to head again. Head contained 57 public tables. The
  frozen initial migration is explicit and independent of live ORM metadata.
  SQLite completed the same head/base/head chain as an additional portability
  check.
- Negative privilege checks proved API and worker DML while denying DDL, denied
  backup-role writes, denied the restore-test role access to the production
  database, and confirmed the bootstrap role becomes `NOLOGIN`.
- Ruff passed; mypy reported no issues in 76 source files; all six generated
  shared contract files were current; `validate_release.py` and the live-schema
  firmware contract validator passed. The cross-repository validator regenerated
  server schemas/OpenAPI in memory and checked firmware fixtures, endpoints,
  downloads, OTA canonical bytes, destructive commands, credential rotation,
  and locked schemas.

### PDF boundary, rate source, and UI

- The production API image enforced the bill-parser boundary and returned
  `{"pdf_sandbox":"enforced","schema_id":"pm-pdf-sandbox-health/1.0.0"}`.
  The full suite covers the closed rate-only schema, prohibited-value discard,
  zero reading/interval/rollup/History creation, same-rate/different-usage
  invariance, unchanged-cost invariance, and diagnostics/backup redaction.
- Official-rate tests cover pinned-IP TLS hostname verification, DNS-rebinding
  rejection, redirects, deadlines, header/body limits, conditional 304 and
  duplicate-200 behavior, immutable candidates/diffs/failures, alerts, and
  weekly scheduling. A verified live fetch of the allowlisted SCE page returned
  HTTP 200 and SHA-256
  `f1e42bb9f0adac1760b88f18b962b36f681db6f22973bbb3891c5ca8b27b80af`;
  the parser returned `HOLIDAY_RULE_MISSING`, so no incomplete candidate was
  guessed or published.
- Frontend lint/type/build checks passed, all 16 Vitest tests passed, and all 19
  Playwright tests passed against the production build. The 1680x946 in-app
  browser acceptance check matched the supplied dashboard composition, emitted
  no console errors, and verified that Format SD commit remains unavailable
  until authenticated device readiness follows prepare.

### Containers, backup, and restore

- Final local images were built and exercised as restricted users with read-only
  roots, dropped capabilities, `no-new-privileges`, and owned tmpfs paths:
  API `sha256:69411146a6969374529208415b931dfed3e16d7eaced3c935c54bb9cb75c63c4`,
  frontend `sha256:c77152237ec2c0653f164007f80f0b158220e7834cae64eb66d970440a530a75`,
  gateway amd64 `sha256:38b9ea3553dad7423ca6dbb1722b227dbdc15d826a9c1ab713a3d370b02c819a`,
  gateway arm64 `sha256:c145ceee27e0b18594ab88875318faeeb4ff4582fb3bf341f370d247c86c259c`,
  and backup `sha256:0a7748ddb6ac514b5ab49d8c581e3de7794708095673ec36ecf6cb524201593d`.
  These are local Docker image IDs, not GHCR manifest digests.
- The gateway image reproducibly compiled the locked standard-module-only Caddy
  2.11.4 source twice with Go 1.26.6 and byte-compared the outputs. Build info
  proved exact `x/net 0.56.0`, `x/text 0.39.0`, and `grpc 1.82.1`; pinned Trivy
  0.72.0 reported zero HIGH/CRITICAL findings in both the amd64 and arm64 final
  images. The image had no file capability on `/usr/bin/caddy`, declared
  `USER 1000:1000`, and its configuration validated with a read-only root, all
  capabilities dropped, and `no-new-privileges`.
- The root Compose gateway profile used host TLS files owned by numeric UID 1000
  at mode 0440. Its named `/data`, `/config`, and `/var/log/powermeter` mounts
  retained image-initialized UID/GID 1000 and mode 0750 and were writable by the
  non-root process. In a fresh disposable production bind-mount recreation, all
  six long-run services became healthy, migration exited 0, the CA-validated
  HTTPS health probe succeeded, and routed readiness reported database ready and
  the PDF sandbox enforced.
- Backup run `20260814T034111Z-55e55c1d6d34` created encrypted archive
  `powermeter-20260814T034111Z-55e55c1d6d34.dump.gpg` (23,469 bytes; ciphertext
  SHA-256 `24969e1a3e0321ae7253ab4f79b8bb90371d1a595a730b3676e8994a01bf2ca3`).
  Healthcheck passed. Isolated restore run
  `restore-20260814T034126Z-bd8d053787f1` verified PostgreSQL 17.10, migration
  `20260813_0007`, 57 public tables, and five checks, then dropped its temporary
  database.
- A separate operator-style restore run
  `restore-20260814T034411Z-4feedd8b8ab7` restored to disposable database
  `pm_restore_manual_test`; a direct query recovered the exact seeded row
  `00000000-0000-0000-0000-000000000001 | Backup evidence home`. The database
  was dropped, zero `pm_restore_%` databases remained, and the exact disposable
  Compose project, its containers, three labeled volumes, and network were
  removed. This cleanup intentionally made the test-only data unrecoverable.

### Historical firmware snapshot

- Firmware repository commit
  `5dea90d91ecd5731b4286a5f67117741aa2ce539` passed 55/55 host tests,
  36/36 fault-injection cases, 63/63 production-C assertions, and the same 63/63
  under ASan/UBSan. Its accelerated 120-day simulation processed 10,368,000
  one-second samples and 172,800 durable intervals.
- Two clean ESP-IDF 6.0.2 release builds were byte-identical. `firmware.bin` is
  978,576 bytes with SHA-256
  `02e0c46a0bfee4fcf35a0bf82de191bf04e69a65d387fbbdbb78e6876b6b06da`.
  The local 24-file candidate pack includes checksums, compatibility metadata,
  SBOM, provenance, memory/stack/test reports, binaries, and PowerShell tools.
  Its manifest and hardware record correctly say `pending`.

### Gates that remain closed

- Signed public server/firmware rc.1 releases and signed public firmware rc.2
  are historical evidence. The signed server rc.2 tag's run `31866197054`
  failed the cross-repository OpenAPI-hash check before server images or release
  assets were published. Current coordinated firmware/server rc.3 publication
  remains pending.
- Public rc.1 GHCR digests, attestations, generated TrueNAS YAML, and release
  smoke are version-specific historical evidence. There is no server rc.2
  image set or YAML, and rc.3 images, anonymous digest resolution, attestations,
  real-digest YAML, and smoke do not exist until its release workflow passes.
  The checked-in template correctly retains `UNPUBLISHED_*` sentinels.
- The target-TrueNAS rc.3 clean install, forward upgrade, restored rollback,
  restart, and permission suite has not run.
- No marked-unit PZEM/ESP32-S3/SD identity, electrical, TLS/HMAC, OTA rollback,
  physical-cycle, USB-recovery, or continuous 72-hour soak evidence exists.
  Simulation cannot satisfy those gates, so stable promotion remains blocked.
