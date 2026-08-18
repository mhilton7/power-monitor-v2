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
Candidate rc.16 retains `pm-protocol/1.0.0` and migration head `20260817_0016`,
adds named service branches, coverage-aware History and billing, synchronization
diagnostics, and coordinated build identity, and has generated
OpenAPI SHA-256
`8c6d3d73f7bfaa4bd34b4451c860b4199426e556cba1f6f9a48374ea22049c24`.
Counts, hashes, artifacts, and Docker image IDs remain
evidence only for the version and workflow phase that produced them and must
not be relabeled as results for a later release.

| Section | Requirement/evidence | Status |
|---|---|---|
| 0 | Fixed product/protocol/time defaults in `backend/app/constants.py`; four fixed GHCR image names and generated release-manifest contract | implemented and statically validated; prior public and failed-release image digests remain immutable version-specific evidence, while rc.16 is the current candidate |
| 1 | `docs/BASELINE_AUDIT.md`, `docs/DEPENDENCY_AUDIT.md`, `docs/MIGRATION.md`; legacy repositories preserved read-only | audited; prior releases remain immutable, and candidate rc.16 awaits its own publication evidence |
| 2 | PZEM-only authority and bill-rate-only boundary in architecture, closed schemas/services, invariance tests, and UI copy | implemented; the full backend real-PostgreSQL suite passed 135 tests with 3 expected environment skips |
| 3 | Monitoring-only exclusions and maintenance-sleep semantics in architecture/commands/security | implemented and host-tested; physical firmware evidence pending |
| 4 | Hardware identity/wiring artifacts in independent firmware repository | documented; actual marked-unit identity/electrical evidence pending |
| 5 | Independent native ESP-IDF 6.0.2 firmware repository and exact dependency pins | historical rc.1 snapshot built twice byte-identically at commit `5dea90d`; public rc.6 carries the main-stack repair; firmware rc.15 is immutable and coordinated rc.16 plus physical evidence remain pending |
| 6 | Deterministic firmware states/tasks plus fixed resource ownership | 55 host and 36 fault cases passed; physical stack/heap/watchdog evidence pending |
| 7 | USB JSON-lines provisioning/repair/flash utilities and transactional recovery | host-tested and packaged; physical COM/USB evidence pending |
| 8 | PZEM V4 parsing/range/CRC/reset/energy evidence and server schema | host/fault-tested; authenticated marked-unit readings pending |
| 9 | UTC/time-trust rules in server and firmware state/tests | implemented and simulated; physical clock-transition evidence pending |
| 10 | Authoritative microSD segmented journal, monotonic sequence/ack, corruption/full-card recovery | host/fault/simulation passed; physical SD/endurance/power-loss evidence pending |
| 11 | Outbound TLS/recovery/heartbeat priority | contracts, host/fault simulation, and server/gateway side passed; marked-unit outage/TLS evidence pending |
| 12 | `pm-protocol/1.0.0`, HMAC/HKDF/replay/dedupe contracts and generated schemas | prior validation is historical; candidate rc.16 keeps the protocol unchanged and requires firmware rc.16 to declare OpenAPI SHA-256 `8c6d3d73f7bfaa4bd34b4451c860b4199426e556cba1f6f9a48374ea22049c24` |
| 13 | Durable outbound command lifecycle, OTA/destructive prepare/commit, credential rotation, and recovery | server command contracts remain authenticated; rc.14 adds per-sensor OTA batches, bounded stage deadlines, startup reconciliation, exact post-reboot version confirmation, rollback/mismatch evidence, and safe retry/cancel coverage; marked-unit command round trips remain pending |
| 14 | OTA metadata/download/manifest/queue/rollback implementation and local release pack | rc.14 separates immutable artifact, batch, and per-sensor attempt state; simulator tests prove one success plus one old-version failure becomes partial, retries only the outdated sensor, preserves attempt history, and never treats delivery as installation success; no production sensor was targeted by these tests |
| 15 | FastAPI/PostgreSQL/React/Caddy Compose structure and exact dependency pins | implemented; prior GHCR images remain historical evidence; current rc.16 source retains the eight-service one-shot initializer and requires its own release build/scan |
| 16 | immutable PZEM ingestion/cost lineage, bill separation, and frozen explicit migrations through current `20260817_0016` | migration 0015 adds nullable structured SCE threshold evidence and explicit legacy OTA batches; migration 0016 generalizes verified circuits into named service branches, safely designates an eligible existing aggregate as Main service/home total, and adds membership/audit metadata without rewriting any reading, device, rate, credential, or firmware artifact row |
| 17 | sessions/MFA/CSRF/throttling/roles/last-owner/home scope/server permissions, including authorized first-sensor home discovery with no enrolled devices and explicit multi-home selection | public rc.3 already exposed authorized scopes with the sensor list before first enrollment; later source adds the permission-independent `/home-scopes` authority and exact-home isolation, retained in rc.16 |
| 18 | exact four-item navigation, responsive/accessibility surfaces, supplied dashboard composition | 31 Vitest and 36 production Playwright tests passed, including eight required viewports, axe checks, focus/dialog behavior, rate workflow, settings persistence, and visual regression coverage |
| 19 | signed live heartbeat, committed History, SSE, dedupe, cost, and guarded device commands | automated API/frontend round trips passed; physical device round trip pending |
| 20 | Decimal/NUMERIC rate engine and official-source pinned-IP transport/sync | the SCE parser now independently reconciles 579 kWh Tier 1 + 372 kWh Tier 2 = 951 kWh, derives and persists an exact 19.3 kWh/day summer boundary, prorates it by actual billing-cycle days, and splits crossing intervals in the existing Decimal cost engine; 28/30/31-day and $354.145410 source-bill regressions pass |
| 21 | isolated closed bill-rate parser, prohibited-field discard, no original-PDF retention, review/publish separation, and invariance | a sanitized generated PDF proves exact rate/threshold extraction, optional service dates, manual missing-day correction, authorized publication, and unchanged sensor-history counts; the supplied private PDF remains local and untracked |
| 22 | sensor-only estimate scopes/disclosures and immutable selected-cost lineage | implemented and covered by exact arithmetic/provenance tests; production data remains unavailable until physical sensor enrollment |
| 23 | all typed alerts plus debounce/ack/silence/maintenance/resolution and checksummed redacted bundle | implemented and covered by alert/diagnostics tests; operator bundle evidence on deployed system pending |
| 24 | encrypted PostgreSQL backup, manifests/status, hash/catalog/decrypt verification, isolated/operator restore | local PostgreSQL 17.10 backup, auto restore, exact-row manual restore, healthcheck, and cleanup passed; target-TrueNAS evidence pending |
| 25 | exact services, internal DB, HTTPS only, UIDs/ACLs, file secrets, hardening, and renderer | public rc.6 exercised and published the eight-service model; rc.16 retains it unchanged, while target-TrueNAS execution remains pending |
| 26 | pinned-action CI/security/release/stable workflows, public-package checks, OCI labels, SBOM/scan/attestations | prior executions remain version-specific evidence; rc.16 requires its own immutable execution |
| 27 | server/frontend/deployment/firmware gates and evidence schemas | rc.16 gates cover additive service-branch, History, billing-tier, synchronization-diagnostic, build-identity, migration upgrade/downgrade, backend/frontend, and release-static validation; generated OpenAPI SHA-256 is `8c6d3d73f7bfaa4bd34b4451c860b4199426e556cba1f6f9a48374ea22049c24`, and coordinated firmware/server rc.16 must bind that exact document before publication; firmware rc.15 remains immutable |
| 28 | measurable acceptance criteria mapped to local automated and physical/deployment suites | feasible automated portions passed; target TrueNAS and marked-unit acceptance remain pending |
| 29 | all required server docs exist in `docs/`; TrueNAS operator docs in `deploy/truenas/` | implemented; screenshots intentionally replaced with exact text diagrams/commands where appropriate |
| 30 | server prerelease workflow plus firmware release packs; stable workflow binds exact marked-unit record to firmware hash/commit and >=72-hour soak | prior coordinated releases remain immutable evidence; coordinated rc.16 and physical certification remain pending separate evidence |
| 31 | feature branches, logical commit/gate/stable-prohibition policy, and CODEOWNERS | public histories and prior immutable tags remain version-specific evidence; current rc.16 source remains untagged and pending review |
| 32 | final-report fields represented by release manifest/reports/traceability | prior release and failed-run evidence remains immutable; rc.16 publication, target deployment, and HIL fields remain unavailable and must not be inferred |
| 33 | definition of done remains open for candidate rc.16 until its coordinated firmware publication, target-TrueNAS recovery testing, and marked-unit certification exist | prior server releases prove distribution only for their own immutable assets; rc.16 and stable gates remain fail-closed until their own evidence exists |

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
