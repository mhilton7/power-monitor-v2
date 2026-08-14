# Requirements traceability

Status vocabulary: **implemented** means linked code and automated evidence exist; **documented/pending test** means the design/operator artifact exists but a runtime gate has not yet produced evidence; **pending physical/external** means hardware, TrueNAS, GitHub, GHCR, or a coordinated repository is required. No row is complete from prose alone.

| Section | Requirement/evidence | Status |
|---|---|---|
| 0 | Fixed product/protocol/time defaults in `backend/app/constants.py`; three fixed GHCR image names and generated release-manifest contract | implemented and statically validated; registry publication pending |
| 1 | `docs/BASELINE_AUDIT.md`, `docs/DEPENDENCY_AUDIT.md`, `docs/MIGRATION.md`; legacy repositories preserved read-only | audited; target Git publication pending |
| 2 | PZEM-only authority and bill-rate-only boundary in architecture, closed schemas/services, invariance tests, and UI copy | implemented; local 105-test suite passed 101 with four expected environment skips |
| 3 | Monitoring-only exclusions and maintenance-sleep semantics in architecture/commands/security | implemented and host-tested; physical firmware evidence pending |
| 4 | Hardware identity/wiring artifacts in independent firmware repository | documented; actual marked-unit identity/electrical evidence pending |
| 5 | Independent native ESP-IDF 6.0.2 firmware repository and exact dependency pins | built twice byte-identically at commit `5dea90d`; public release pending |
| 6 | Deterministic firmware states/tasks plus fixed resource ownership | 55 host and 36 fault cases passed; physical stack/heap/watchdog evidence pending |
| 7 | USB JSON-lines provisioning/repair/flash utilities and transactional recovery | host-tested and packaged; physical COM/USB evidence pending |
| 8 | PZEM V4 parsing/range/CRC/reset/energy evidence and server schema | host/fault-tested; authenticated marked-unit readings pending |
| 9 | UTC/time-trust rules in server and firmware state/tests | implemented and simulated; physical clock-transition evidence pending |
| 10 | Authoritative microSD segmented journal, monotonic sequence/ack, corruption/full-card recovery | host/fault/simulation passed; physical SD/endurance/power-loss evidence pending |
| 11 | Outbound TLS/recovery/heartbeat priority | contracts, host/fault simulation, and server/gateway side passed; marked-unit outage/TLS evidence pending |
| 12 | `pm-protocol/1.0.0`, HMAC/HKDF/replay/dedupe contracts and generated schemas | server tests and live-schema cross-repository validator passed; public coordinated workflow pending |
| 13 | Durable outbound command lifecycle, OTA/destructive prepare/commit, credential rotation, and recovery | server and firmware host contracts passed; marked-unit command round trips pending |
| 14 | OTA metadata/download/manifest/queue/rollback implementation and local release pack | built and contract-tested; signed tag, public artifacts, and physical OTA rollback pending |
| 15 | FastAPI/PostgreSQL/React/Caddy Compose structure and exact dependency pins | implemented; final API/frontend/backup local images passed restricted-runtime checks; CI registry build pending |
| 16 | immutable PZEM ingestion/cost lineage, bill separation, and frozen explicit migrations through `20260813_0007` | local PostgreSQL 17.10 head/base/head and 102-pass role-split suite passed; tagged release run pending |
| 17 | sessions/MFA/CSRF/throttling/roles/last-owner/home scope/server permissions | implemented and covered in passing local suite; external CI pending |
| 18 | exact four-item navigation, responsive/accessibility surfaces, supplied dashboard composition | 13 Vitest and 18 production Playwright tests passed; 1680x946 visual/browser acceptance passed |
| 19 | signed live heartbeat, committed History, SSE, dedupe, cost, and guarded device commands | automated API/frontend round trips passed; physical device round trip pending |
| 20 | Decimal/NUMERIC rate engine and official-source pinned-IP transport/sync | deterministic tests passed; live public page failed closed with `HOLIDAY_RULE_MISSING`, so no rate candidate was guessed |
| 21 | isolated closed bill-rate parser, prohibited-field discard, review/publish separation, and invariance | local invariance/API/redaction tests and production-container sandbox check passed; tagged Linux CI evidence pending |
| 22 | sensor-only estimate scopes/disclosures and immutable selected-cost lineage | implemented and covered by exact arithmetic/provenance tests; production data remains unavailable until physical sensor enrollment |
| 23 | all typed alerts plus debounce/ack/silence/maintenance/resolution and checksummed redacted bundle | implemented and covered by alert/diagnostics tests; operator bundle evidence on deployed system pending |
| 24 | encrypted PostgreSQL backup, manifests/status, hash/catalog/decrypt verification, isolated/operator restore | local PostgreSQL 17.10 backup, auto restore, exact-row manual restore, healthcheck, and cleanup passed; target-TrueNAS evidence pending |
| 25 | exact seven services, internal DB, HTTPS only, UIDs/ACLs, file secrets, hardening, and renderer | static and local component runtime checks passed; public real-digest seven-service/target-TrueNAS execution pending |
| 26 | pinned-action CI/security/release/stable workflows, public-package checks, OCI labels, SBOM/scan/attestations | workflows and local audits validated; target repos are absent and invalid `gh` authentication plus no configured signing key/tool block GitHub execution/publication |
| 27 | server/frontend/deployment/firmware gates and evidence schemas | local server 101/4, PostgreSQL 102/3, frontend 13/18, firmware 55/36/63+63, and 120-day simulation passed; release smoke/HIL pending |
| 28 | measurable acceptance criteria mapped to local automated and physical/deployment suites | feasible automated portions passed; target TrueNAS and marked-unit acceptance remain pending |
| 29 | all required server docs exist in `docs/`; TrueNAS operator docs in `deploy/truenas/` | implemented; screenshots intentionally replaced with exact text diagrams/commands where appropriate |
| 30 | server prerelease workflow plus local 24-file firmware pack; stable workflow binds exact marked-unit record to firmware hash/commit and >=72-hour soak | tooling/local firmware artifacts verified; signed/public releases, registry artifacts, and physical certification pending |
| 31 | feature branches, logical commit/gate/stable-prohibition policy, and CODEOWNERS | firmware branch committed locally; target GitHub repos are absent and invalid authentication/no signing key block push, draft PR, and signed tags |
| 32 | final-report fields represented by release manifest/reports/traceability | local evidence assembled; public URLs/digests/target deployment/HIL fields remain unavailable and must not be inferred |
| 33 | definition of done remains open because no signed public release/GHCR digests, real-digest TrueNAS smoke, target-TrueNAS suite, or marked-unit certification exists | explicitly fail-closed; candidate only |

## Deployment/security test mapping

| Requirement | Automation/artifact |
|---|---|
| YAML syntax, exact services, port/network/hardening/file-secret policy | `scripts/validate_release.py`, `.github/workflows/ci.yml` Compose job |
| Real digest substitution and release manifest integrity | `scripts/render_truenas_release.py`, `scripts/verify_release_artifacts.py` |
| Clean migration/backend/integration | `.github/workflows/ci.yml`, `.github/workflows/release-gates.yml` |
| Container builds and metadata | CI containers matrix; tagged release multi-architecture matrix |
| Vulnerability/dependency/secret/static scans | `.github/workflows/security.yml`, release Trivy scans |
| SBOM/provenance | release Anchore SPDX and `actions/attest` per digest/artifact |
| Backup integrity and restore | `backup/backup.sh`, `backup/restore.sh`, status evidence consumed by API |
| TrueNAS install/upgrade/rollback/permissions/restarts | exact procedures in `deploy/truenas`; execution pending target host |
| Cross-repo protocol and firmware host validation | `.github/workflows/firmware-contract.yml`; HIL is explicitly separate |
| Stable physical gate | `scripts/verify_hardware_certification.py`, `.github/workflows/stable-promotion.yml`; cannot pass without the firmware schema, matching binary/commit, marked-unit photos, every required test, and a >=72-hour passing soak |

Generated reports must contain exact inputs/checksums and status. This document must be updated from **pending** only after the linked evidence exists; a file path or planned workflow is not a test result.
