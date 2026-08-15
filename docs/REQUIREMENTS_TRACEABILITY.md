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
attached assets/instructions. Candidate rc.4 binds the audited source to
migration head `20260815_0011`, firmware rc.4, and generated OpenAPI SHA-256
`f9b936468f5a696a0bee3e04edda021c12ab81dddc091cbb307face0be1de7b1`.
Counts, hashes, artifacts, and Docker image IDs remain
evidence only for the version and workflow phase that produced them and must
not be relabeled as results for a later release.

| Section | Requirement/evidence | Status |
|---|---|---|
| 0 | Fixed product/protocol/time defaults in `backend/app/constants.py`; four fixed GHCR image names and generated release-manifest contract | implemented and statically validated; public rc.1 and rc.3 GHCR digests are immutable historical release evidence, while rc.4 is the coordinated audited candidate |
| 1 | `docs/BASELINE_AUDIT.md`, `docs/DEPENDENCY_AUDIT.md`, `docs/MIGRATION.md`; legacy repositories preserved read-only | audited; coordinated rc.1/rc.3 releases are public, rc.2 server publication failed closed, and candidate rc.4 awaits its own immutable publication evidence |
| 2 | PZEM-only authority and bill-rate-only boundary in architecture, closed schemas/services, invariance tests, and UI copy | implemented; the full backend real-PostgreSQL suite passed 135 tests with 3 expected environment skips |
| 3 | Monitoring-only exclusions and maintenance-sleep semantics in architecture/commands/security | implemented and host-tested; physical firmware evidence pending |
| 4 | Hardware identity/wiring artifacts in independent firmware repository | documented; actual marked-unit identity/electrical evidence pending |
| 5 | Independent native ESP-IDF 6.0.2 firmware repository and exact dependency pins | historical rc.1 snapshot built twice byte-identically at commit `5dea90d`; signed public firmware rc.1 and rc.2 candidates exist, with physical evidence still pending |
| 6 | Deterministic firmware states/tasks plus fixed resource ownership | 55 host and 36 fault cases passed; physical stack/heap/watchdog evidence pending |
| 7 | USB JSON-lines provisioning/repair/flash utilities and transactional recovery | host-tested and packaged; physical COM/USB evidence pending |
| 8 | PZEM V4 parsing/range/CRC/reset/energy evidence and server schema | host/fault-tested; authenticated marked-unit readings pending |
| 9 | UTC/time-trust rules in server and firmware state/tests | implemented and simulated; physical clock-transition evidence pending |
| 10 | Authoritative microSD segmented journal, monotonic sequence/ack, corruption/full-card recovery | host/fault/simulation passed; physical SD/endurance/power-loss evidence pending |
| 11 | Outbound TLS/recovery/heartbeat priority | contracts, host/fault simulation, and server/gateway side passed; marked-unit outage/TLS evidence pending |
| 12 | `pm-protocol/1.0.0`, HMAC/HKDF/replay/dedupe contracts and generated schemas | rc.3 historical validation passed; candidate rc.4 keeps the protocol unchanged and requires firmware rc.4 to declare OpenAPI SHA-256 `f9b936468f5a696a0bee3e04edda021c12ab81dddc091cbb307face0be1de7b1` |
| 13 | Durable outbound command lifecycle, OTA/destructive prepare/commit, credential rotation, and recovery | server and firmware host contracts passed; marked-unit command round trips pending |
| 14 | OTA metadata/download/manifest/queue/rollback implementation and local release pack | historical rc.1 build/contract evidence and signed public firmware rc.1/rc.2 releases exist; marked-unit OTA rollback remains pending |
| 15 | FastAPI/PostgreSQL/React/Caddy Compose structure and exact dependency pins | implemented; public rc.1/rc.3 GHCR images are historical evidence; current source adds the exact API-image one-shot initializer and requires a new release build/scan |
| 16 | immutable PZEM ingestion/cost lineage, bill separation, and frozen explicit migrations through current `20260815_0011` | candidate rc.4 adds 0008 fail-closed raw/permanent-loss overlap guards and 0010 permanent-loss UPDATE/DELETE immutability without rewriting evidence; the real PostgreSQL 17 clean chain through 0011 and 0011-to-0010-to-head exercise passed locally; historical rc.3 remains at `20260813_0007`, and the tagged forward public-rc.3 upgrade and rollback remain separate |
| 17 | sessions/MFA/CSRF/throttling/roles/last-owner/home scope/server permissions, including authorized first-sensor home discovery with no enrolled devices and explicit multi-home selection | public rc.3 already exposed authorized scopes with the sensor list before first enrollment; candidate rc.4 adds the permission-independent `/home-scopes` authority and exact-home isolation, covered by backend and frontend zero/one/multiple-scope tests |
| 18 | exact four-item navigation, responsive/accessibility surfaces, supplied dashboard composition | 24 Vitest and 36 production Playwright tests passed, including eight required viewports, axe checks, focus/dialog behavior, rate workflow, and visual regression coverage |
| 19 | signed live heartbeat, committed History, SSE, dedupe, cost, and guarded device commands | automated API/frontend round trips passed; physical device round trip pending |
| 20 | Decimal/NUMERIC rate engine and official-source pinned-IP transport/sync | migrations 0009/0011 and 20/20 PostgreSQL workflow tests cover exact-home status/LKG, database-idempotent manual candidates, immutable provenance, review/publish/activate or review/reject lifecycle, shared serialized bill/SCE versions, non-overlapping assignments/equal-start rejection, advisory leasing, bounded retry, durable validators, and per-home alerts; the live page still failed closed with `HOLIDAY_RULE_MISSING`, so no rate was guessed |
| 21 | isolated closed bill-rate parser, prohibited-field discard, no original-PDF retention, review/publish separation, and invariance | current source discards original bill bytes and all prohibited fields after rate-only parsing; historical local invariance/API/redaction and published rc.1/rc.3 production-container evidence remain version-specific |
| 22 | sensor-only estimate scopes/disclosures and immutable selected-cost lineage | implemented and covered by exact arithmetic/provenance tests; production data remains unavailable until physical sensor enrollment |
| 23 | all typed alerts plus debounce/ack/silence/maintenance/resolution and checksummed redacted bundle | implemented and covered by alert/diagnostics tests; operator bundle evidence on deployed system pending |
| 24 | encrypted PostgreSQL backup, manifests/status, hash/catalog/decrypt verification, isolated/operator restore | local PostgreSQL 17.10 backup, auto restore, exact-row manual restore, healthcheck, and cleanup passed; target-TrueNAS evidence pending |
| 25 | exact services, internal DB, HTTPS only, UIDs/ACLs, file secrets, hardening, and renderer | published rc.1/rc.3 seven-service smokes are historical; current source validates an eight-service no-shell initializer model, but its target-TrueNAS execution and new tagged release remain pending |
| 26 | pinned-action CI/security/release/stable workflows, public-package checks, OCI labels, SBOM/scan/attestations | public rc.1/rc.3 releases and packages exist; failed server rc.2 run `31866197054` published none; candidate rc.4 workflow/dependency repairs require their own immutable execution |
| 27 | server/frontend/deployment/firmware gates and evidence schemas | published rc.1/rc.3 gate and smoke evidence is immutable; current audit adds initializer/stager/static evidence locally, while target TrueNAS and HIL remain pending |
| 28 | measurable acceptance criteria mapped to local automated and physical/deployment suites | feasible automated portions passed; target TrueNAS and marked-unit acceptance remain pending |
| 29 | all required server docs exist in `docs/`; TrueNAS operator docs in `deploy/truenas/` | implemented; screenshots intentionally replaced with exact text diagrams/commands where appropriate |
| 30 | server prerelease workflow plus firmware release packs; stable workflow binds exact marked-unit record to firmware hash/commit and >=72-hour soak | coordinated signed public server/firmware rc.1 and rc.3 assets exist; candidate rc.4 and physical certification remain pending their separate evidence |
| 31 | feature branches, logical commit/gate/stable-prohibition policy, and CODEOWNERS | public histories, reviewed PRs, signed rc.1/rc.3 tags, and immutable failed server rc.2 tag exist; current full-audit source changes remain untagged and pending review |
| 32 | final-report fields represented by release manifest/reports/traceability | public rc.1/rc.3 URLs/digests exist and server rc.2 has only a failed-run URL; rc.4 publication, target deployment, and HIL fields remain unavailable and must not be inferred |
| 33 | definition of done remains open for candidate rc.4 until its coordinated publication, target-TrueNAS recovery testing, and marked-unit certification exist | public rc.3 proves distribution for its immutable seven-service assets only; rc.4's eight-service/no-shell model and stable gates remain fail-closed until their own evidence exists |

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
