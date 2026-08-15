# Requirements traceability

Status vocabulary: **implemented** means linked code and automated evidence exist; **documented/pending test** means the design/operator artifact exists but a runtime gate has not yet produced evidence; **pending physical/external** means hardware, TrueNAS, or current-candidate publication evidence is still required. No row is complete from prose alone.

Publication state is deliberately versioned: signed public server and firmware
`v0.1.0-rc.1` releases are historical candidate evidence, and server rc.1
remains the installation authority. Signed public firmware `v0.1.0-rc.2` is
historical. Signed server tag `v0.1.0-rc.2` and failed run `31866197054` are
immutable prepublication failure evidence: the cross-repository OpenAPI hash
check failed before any server rc.2 Release, image set, YAML, or deployment
smoke was published. Server and firmware `v0.1.0-rc.3` are the current
coordinated target. Counts, hashes, artifacts, and Docker image IDs remain
evidence only for the version and workflow phase that produced them and must
not be relabeled as rc.3 release results.

| Section | Requirement/evidence | Status |
|---|---|---|
| 0 | Fixed product/protocol/time defaults in `backend/app/constants.py`; four fixed GHCR image names and generated release-manifest contract | implemented and statically validated; public rc.1 GHCR digests are historical release evidence, while server rc.3 registry publication is pending |
| 1 | `docs/BASELINE_AUDIT.md`, `docs/DEPENDENCY_AUDIT.md`, `docs/MIGRATION.md`; legacy repositories preserved read-only | audited; both new repository histories and rc.1 releases are public, rc.2 server publication failed closed, and rc.3 publication is pending |
| 2 | PZEM-only authority and bill-rate-only boundary in architecture, closed schemas/services, invariance tests, and UI copy | implemented; local 105-test suite passed 101 with four expected environment skips |
| 3 | Monitoring-only exclusions and maintenance-sleep semantics in architecture/commands/security | implemented and host-tested; physical firmware evidence pending |
| 4 | Hardware identity/wiring artifacts in independent firmware repository | documented; actual marked-unit identity/electrical evidence pending |
| 5 | Independent native ESP-IDF 6.0.2 firmware repository and exact dependency pins | historical rc.1 snapshot built twice byte-identically at commit `5dea90d`; signed public firmware rc.1 and rc.2 candidates exist, with physical evidence still pending |
| 6 | Deterministic firmware states/tasks plus fixed resource ownership | 55 host and 36 fault cases passed; physical stack/heap/watchdog evidence pending |
| 7 | USB JSON-lines provisioning/repair/flash utilities and transactional recovery | host-tested and packaged; physical COM/USB evidence pending |
| 8 | PZEM V4 parsing/range/CRC/reset/energy evidence and server schema | host/fault-tested; authenticated marked-unit readings pending |
| 9 | UTC/time-trust rules in server and firmware state/tests | implemented and simulated; physical clock-transition evidence pending |
| 10 | Authoritative microSD segmented journal, monotonic sequence/ack, corruption/full-card recovery | host/fault/simulation passed; physical SD/endurance/power-loss evidence pending |
| 11 | Outbound TLS/recovery/heartbeat priority | contracts, host/fault simulation, and server/gateway side passed; marked-unit outage/TLS evidence pending |
| 12 | `pm-protocol/1.0.0`, HMAC/HKDF/replay/dedupe contracts and generated schemas | server tests and live-schema cross-repository validator passed; rc.2 rejected a stale OpenAPI digest, and rc.3 requires coordinated firmware rc.3 to declare server OpenAPI SHA-256 `7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c` |
| 13 | Durable outbound command lifecycle, OTA/destructive prepare/commit, credential rotation, and recovery | server and firmware host contracts passed; marked-unit command round trips pending |
| 14 | OTA metadata/download/manifest/queue/rollback implementation and local release pack | historical rc.1 build/contract evidence and signed public firmware rc.1/rc.2 releases exist; marked-unit OTA rollback remains pending |
| 15 | FastAPI/PostgreSQL/React/Caddy Compose structure and exact dependency pins | implemented; historical local restricted-runtime checks and public rc.1 GHCR images exist, while rc.3 registry builds are pending |
| 16 | immutable PZEM ingestion/cost lineage, bill separation, and frozen explicit migrations through `20260813_0007` | historical local PostgreSQL 17.10 head/base/head and 102-pass role-split suite plus published rc.1 migration evidence exist; the rc.3 gate must select public rc.1 and prove only forward rc.1-to-rc.3 upgrade, never rollback compatibility |
| 17 | sessions/MFA/CSRF/throttling/roles/last-owner/home scope/server permissions, including authorized first-sensor home discovery with no enrolled devices and explicit multi-home selection | implemented and covered by backend isolation and frontend single/multi/zero-scope tests; server rc.3 publication remains pending |
| 18 | exact four-item navigation, responsive/accessibility surfaces, supplied dashboard composition | 16 Vitest and 19 production Playwright tests passed; 1680x946 visual/browser acceptance passed |
| 19 | signed live heartbeat, committed History, SSE, dedupe, cost, and guarded device commands | automated API/frontend round trips passed; physical device round trip pending |
| 20 | Decimal/NUMERIC rate engine and official-source pinned-IP transport/sync | deterministic tests passed; live public page failed closed with `HOLIDAY_RULE_MISSING`, so no rate candidate was guessed |
| 21 | isolated closed bill-rate parser, prohibited-field discard, review/publish separation, and invariance | historical local invariance/API/redaction and production-container sandbox checks passed; published rc.1 and partial failed-run rc.2 evidence remain version-specific, while rc.3 tagged Linux evidence is pending |
| 22 | sensor-only estimate scopes/disclosures and immutable selected-cost lineage | implemented and covered by exact arithmetic/provenance tests; production data remains unavailable until physical sensor enrollment |
| 23 | all typed alerts plus debounce/ack/silence/maintenance/resolution and checksummed redacted bundle | implemented and covered by alert/diagnostics tests; operator bundle evidence on deployed system pending |
| 24 | encrypted PostgreSQL backup, manifests/status, hash/catalog/decrypt verification, isolated/operator restore | local PostgreSQL 17.10 backup, auto restore, exact-row manual restore, healthcheck, and cleanup passed; target-TrueNAS evidence pending |
| 25 | exact seven services, internal DB, HTTPS only, UIDs/ACLs, file secrets, hardening, and renderer | static/local checks and the published rc.1 real-digest seven-service smoke are historical evidence; rc.2 smoke was skipped after the contract failure, and rc.3 real-digest smoke plus target-TrueNAS execution remain pending |
| 26 | pinned-action CI/security/release/stable workflows, public-package checks, OCI labels, SBOM/scan/attestations | public repositories, signed rc.1 releases, public rc.1 packages, and signed public firmware rc.2 exist; failed server rc.2 run `31866197054` published no images or release, and rc.3 execution remains pending |
| 27 | server/frontend/deployment/firmware gates and evidence schemas | historical local server 101/4, PostgreSQL 102/3, frontend 16/19, firmware 55/36/63+63, simulation evidence, published rc.1 smoke, and successful portions of failed rc.2 run exist; rc.3 full release gates and HIL remain pending |
| 28 | measurable acceptance criteria mapped to local automated and physical/deployment suites | feasible automated portions passed; target TrueNAS and marked-unit acceptance remain pending |
| 29 | all required server docs exist in `docs/`; TrueNAS operator docs in `deploy/truenas/` | implemented; screenshots intentionally replaced with exact text diagrams/commands where appropriate |
| 30 | server prerelease workflow plus firmware release packs; stable workflow binds exact marked-unit record to firmware hash/commit and >=72-hour soak | signed public server/firmware rc.1 and firmware rc.2 releases exist; coordinated firmware/server rc.3 assets and physical certification remain pending |
| 31 | feature branches, logical commit/gate/stable-prohibition policy, and CODEOWNERS | public histories, reviewed PRs, signed rc.1 tags, signed firmware rc.2, and immutable failed server rc.2 tag exist; rc.3 source preparation remains untagged and pending review |
| 32 | final-report fields represented by release manifest/reports/traceability | public rc.1 URLs/digests and the public firmware rc.2 URL exist; server rc.2 has only a failed run URL, while rc.3 release URLs/digests, target deployment, and HIL fields remain unavailable and must not be inferred |
| 33 | definition of done remains open for the current server candidate until rc.3 release evidence, its real-digest smoke, target-TrueNAS recovery testing, and marked-unit certification exist | published rc.1 proves prerelease distribution only; rc.3 and stable gates remain fail-closed |

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
