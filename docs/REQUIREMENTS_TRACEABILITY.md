# Requirements traceability

Status vocabulary: **implemented** means linked code and automated evidence exist; **documented/pending test** means the design/operator artifact exists but a runtime gate has not yet produced evidence; **pending physical/external** means hardware, TrueNAS, or current-candidate publication evidence is still required. No row is complete from prose alone.

Publication state is deliberately versioned: signed public server and firmware
`v0.1.0-rc.1` releases are historical candidate evidence. Signed public
firmware `v0.1.0-rc.2` is historical. Signed server tag `v0.1.0-rc.2` and
failed run `31866197054` are
immutable prepublication failure evidence: the cross-repository OpenAPI hash
check failed before any server rc.2 Release, image set, YAML, or deployment
smoke was published. Coordinated server and firmware `v0.1.0-rc.3` are public,
immutable historical evidence and server rc.3 is the authority only for its
attached assets/instructions. Signed public firmware rc.4 and the signed server
rc.4 tag are also historical. Server rc.4 run `31893354667` passed the
workflow's named `Mandatory release gates` job, four image jobs, and anonymous
GHCR access, then failed deterministic
deployment smoke; assembly was skipped, so there is no server rc.4 Release or
YAML. Coordinated server and firmware rc.5 and rc.6 are public, and server rc.6
remains installable with its attached assets. Hardware execution confirms
firmware rc.1 through rc.5 crash in the main stack before provisioning.
Public rc.16 remains immutable historical installation evidence. Firmware
rc.17 is immutable failed-candidate evidence because its compatibility asset
omitted the telemetry protocol binding. Firmware rc.22 is immutable public
evidence. Signed server tag rc.22 and run `32451170213` are immutable
failed-candidate evidence: mandatory gates and image publication passed,
deployment smoke failed on an unexecutable holiday-sensitive bill rate, and
assembly was skipped, so no server Release or generated YAML exists. Public
rc.24 is immutable release evidence. Candidate rc.25 retains
`pm-protocol/1.0.0` and stateless
`pm-telemetry/2.0.0`, and advances the migration head to `20260821_0019`.
It makes PostgreSQL the durable telemetry/History owner, removes sensor
microSD/backlog behavior, preserves NVS identity/configuration, and has
generated OpenAPI SHA-256
`f40aed47eb572db1d328e3130fd0a86e6a8c9c123ba244d4cb90db3a4dd039bb`.
Counts, hashes, artifacts, and Docker image IDs remain
evidence only for the version and workflow phase that produced them and must
not be relabeled as results for a later release.

| Section | Requirement/evidence | Status |
|---|---|---|
| 0 | Fixed product/protocol/time defaults in `backend/app/constants.py`; four fixed GHCR image names and generated release-manifest contract | implemented and statically validated; prior public and failed-release image digests remain immutable version-specific evidence, while rc.25 is the current candidate |
| 1 | `docs/BASELINE_AUDIT.md`, `docs/DEPENDENCY_AUDIT.md`, `docs/MIGRATION.md`; legacy repositories preserved read-only | audited; prior releases remain immutable, and candidate rc.25 awaits its own publication evidence |
| 2 | PZEM-only authority and bill-rate-only boundary in architecture, closed schemas/services, invariance tests, and UI copy | implemented; the full backend real-PostgreSQL suite passed 135 tests with 3 expected environment skips |
| 3 | Monitoring-only exclusions and maintenance-sleep semantics in architecture/commands/security | implemented and host-tested; physical firmware evidence pending |
| 4 | Hardware identity/wiring artifacts in independent firmware repository | documented; actual marked-unit identity/electrical evidence pending |
| 5 | Independent native ESP-IDF 6.0.2 firmware repository and exact dependency pins | rc.25 stateless candidate uses the pinned 6.0.2 lock; prior builds remain immutable; its own build and physical evidence remain pending |
| 6 | Deterministic firmware tasks plus one in-flight/one newest-pending fixed-memory ownership | focused contracts, profile checks, authoritative build, and frame audit passed; physical stack/heap/watchdog evidence pending |
| 7 | USB JSON-lines provisioning/repair/flash utilities and transactional recovery | host-tested and packaged; physical COM/USB evidence pending |
| 8 | PZEM V4 parsing/range/CRC/reset/energy evidence and server schema | host/fault-tested; authenticated marked-unit readings pending |
| 9 | UTC/time-trust rules in server and firmware state/tests | implemented and simulated; physical clock-transition evidence pending |
| 10 | No sensor telemetry persistence: microSD is untouched; RAM holds one in-flight and one newest pending sample | implemented and statically/host/build validated; outage and power-cycle marked-unit evidence pending |
| 11 | Outbound verified HTTPS with independent Wi-Fi/server bounded retry and newest-sample replacement | contracts, host validation, and server/gateway side passed; marked-unit outage/TLS evidence pending |
| 12 | `pm-protocol/1.0.0` HMAC/replay plus additive `pm-telemetry/2.0.0` request/response schemas and vectors | candidate rc.25 must bind the generated schemas/vector and exact generated OpenAPI digest in the firmware contract vector |
| 13 | Durable outbound command lifecycle, OTA/destructive prepare/commit, credential rotation, and recovery | server command contracts remain authenticated; rc.14 adds per-sensor OTA batches, bounded stage deadlines, startup reconciliation, exact post-reboot version confirmation, rollback/mismatch evidence, and safe retry/cancel coverage; marked-unit command round trips remain pending |
| 14 | OTA metadata/download/manifest/queue/rollback implementation and local release pack | rc.14 separates immutable artifact, batch, and per-sensor attempt state; simulator tests prove one success plus one old-version failure becomes partial, retries only the outdated sensor, preserves attempt history, and never treats delivery as installation success; no production sensor was targeted by these tests |
| 15 | FastAPI/PostgreSQL/React/Caddy Compose structure and exact dependency pins | implemented; prior GHCR images remain historical evidence; rc.25 retains the eight-service one-shot initializer and requires its own release build/scan |
| 16 | immutable PZEM ingestion/cost lineage, bill separation, and frozen explicit migrations through current `20260821_0019` | revision 0017 adds immutable stateless telemetry; revision 0018 adds rate-catalog and firmware/deployment lifecycle evidence; revision 0019 adds Settings-owned billing calculation configuration without rewriting prior data; downgrades fail closed when newer evidence exists |
| 17 | sessions/MFA/CSRF/throttling/roles/last-owner/home scope/server permissions, including authorized first-sensor home discovery with no enrolled devices and explicit multi-home selection | public rc.3 already exposed authorized scopes with the sensor list before first enrollment; later source adds the permission-independent `/home-scopes` authority and exact-home isolation, retained in rc.16 |
| 18 | exact four-item navigation, responsive/accessibility surfaces, Main-service dashboard/History, stateless sensor UI, and exact-tier Billing | frontend lint/type/build, 68 unit tests, and 42 Playwright desktop/mobile/WCAG tests passed |
| 19 | signed independently accepted telemetry, server-owned History, SSE, idempotency, cost, and guarded commands | automated API/frontend round trips passed; physical device round trip pending |
| 20 | Decimal/NUMERIC rate engine and official-source pinned-IP transport/sync | the SCE parser now independently reconciles 579 kWh Tier 1 + 372 kWh Tier 2 = 951 kWh, derives and persists an exact 19.3 kWh/day summer boundary, prorates it by actual billing-cycle days, and splits crossing intervals in the existing Decimal cost engine; 28/30/31-day and $354.145410 source-bill regressions pass |
| 21 | isolated closed bill-rate parser, prohibited-field discard, no original-PDF retention, review/publish separation, and invariance | a sanitized generated PDF proves exact rate/threshold extraction, optional service dates, manual missing-day correction, authorized publication, and unchanged sensor-history counts; the supplied private PDF remains local and untracked |
| 22 | sensor-only estimate scopes/disclosures and immutable selected-cost lineage | implemented and covered by exact arithmetic/provenance tests; production data remains unavailable until physical sensor enrollment |
| 23 | stateless delivery/PZEM/time/TLS/Wi-Fi/OTA/energy/rate/backup alerts plus debounce/ack/silence/maintenance/resolution and checksummed redacted bundle | implemented and covered by alert/diagnostics tests; operator bundle evidence on deployed system pending |
| 24 | encrypted PostgreSQL backup, manifests/status, hash/catalog/decrypt verification, isolated/operator restore | local PostgreSQL 17.10 backup, auto restore, exact-row manual restore, healthcheck, and cleanup passed; target-TrueNAS evidence pending |
| 25 | exact services, internal DB, HTTPS only, UIDs/ACLs, file secrets, hardening, and renderer | public rc.21 uses the eight-service model; rc.25 retains it, while target-TrueNAS execution remains pending |
| 26 | pinned-action CI/security/release/stable workflows, public-package checks, OCI labels, SBOM/scan/attestations | prior executions remain version-specific evidence; rc.25 requires its own immutable execution |
| 27 | server/frontend/deployment/firmware gates and stateless telemetry evidence schemas | local focused server/frontend/firmware gates pass; final full suites and coordinated rc.25 publication must bind the exact OpenAPI/schema/vector hashes before publication |
| 28 | measurable acceptance criteria mapped to automated and physical/deployment suites | feasible automated portions passed; physical sensor migration, target TrueNAS, and marked-unit acceptance remain pending |
| 29 | all required server docs exist in `docs/`; TrueNAS operator docs in `deploy/truenas/` | implemented; screenshots intentionally replaced with exact text diagrams/commands where appropriate |
| 30 | server prerelease workflow plus firmware release packs; stable workflow binds exact marked-unit record to firmware hash/commit and >=72-hour soak | prior coordinated releases remain immutable evidence; coordinated rc.25 and physical certification remain pending separate evidence |
| 31 | feature branches, logical commit/gate/stable-prohibition policy, and CODEOWNERS | public histories and prior immutable tags remain version-specific evidence; current rc.25 source remains untagged and pending review |
| 32 | final-report fields represented by release manifest/reports/traceability | prior release and failed-run evidence remains immutable; rc.25 publication, target deployment, and HIL fields must not be inferred before execution |
| 33 | definition of done remains open for stable rc.25 until target-TrueNAS recovery testing, explicit one-at-a-time physical migration, and marked-unit certification exist | release-candidate publication may proceed after all nonphysical gates; physical installation is never performed by automated tests |

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
| TrueNAS install/upgrade/rollback/permissions/restarts | exact procedures in `deploy/truenas`; forward upgrade and separate matching-database restore/cutover execution remain pending on the target host |
| Cross-repo protocol and firmware host validation | `.github/workflows/firmware-contract.yml`; HIL is explicitly separate |
| Stable physical gate | `scripts/verify_hardware_certification.py`, `.github/workflows/stable-promotion.yml`; cannot pass without the firmware schema, matching binary/commit, marked-unit photos, every required test, and a >=72-hour passing soak |

Generated reports must contain exact inputs/checksums and status. This document must be updated from **pending** only after the linked evidence exists; a file path or planned workflow is not a test result.
