# Full repository audit

> **Post-audit release update:** Server `v0.1.0-rc.6` subsequently completed
> publication and remains public and installable with its attached assets.
> Hardware execution confirmed that firmware rc.1 through rc.5 crash in the
> main stack before provisioning. Coordinated rc.8 publication is
> pending. The report below preserves the evidence boundary at audit time.

## Audit identity and evidence boundary

This report records the invasive audit performed on 2026-08-15. The server
work began from clean public-rc.3 commit
`0703acb265cf104674c9f54d5d65176a0b899cb3` and is being repaired on
`codex/full-repository-audit`. The independent nested firmware repository began
from `8d91cfdfbae1346e3714d9bd7abb05a16f792c66`. Existing public
`v0.1.0-rc.3` tags, assets, image digests, attestations, and instructions are
immutable historical evidence; none of the working-tree results below are
attributed to rc.3.

At documentation start, the Git inventories contained 254 server-repository
entries and 129 nested-firmware entries. Repair work continued concurrently
while these reports were written, so the audit runner's final per-path output,
not a copied total, is authoritative for the finished tree. The exact
reproducible inventory command is:

```powershell
git ls-files --cached --others --exclude-standard
git -C power-monitor-sensor-headless ls-files --cached --others --exclude-standard
```

The audit runner writes the per-path, timestamped inventory to
`artifacts/audit/<run-id>/first-party-inventory.tsv`. That output is ignored by
Git and is local evidence, not release evidence.

## Scanned first-party areas

| Area | Scope inspected |
| --- | --- |
| Repository policy and build inputs | `AGENTS.md`, ignore rules, environment template, Python and npm manifests/locks, TypeScript, ESLint, Vite, Vitest, Playwright, Alembic, and Compose configuration |
| Backend | FastAPI entry points, every route, request/response schemas, authentication and permissions, models, database access, ingestion, History, pricing, cost lineage, bill-rate sandbox/parser, SCE transport/parser/synchronizer, logging, diagnostics, storage/backup status, migrations, and backend tests |
| Frontend | The complete first-party frontend: application shell, API client and runtime schemas, auth, active-home state, pages, cards, dialogs, charts, forms, styles, test fixtures, unit tests, and browser tests |
| Database | Frozen migrations `0001` through `0007`, additive migrations `20260815_0008` through `20260815_0012`, ORM constraints, transactions, indexes, role initialization, clean/upgrade test definitions, backup, and isolated restore paths |
| Worker and backup | Worker jobs/leases, rollups/cost processing, backup creation, manifest/status reporting, verification, and isolated restore scripts |
| Containers and gateway | API/frontend/gateway/backup Dockerfiles, Caddy configuration, local Compose, production TrueNAS template, health checks, users, mounts, capabilities, tmpfs, secrets, and start ordering |
| TrueNAS and release | Dataset/ACL guides, Windows SMB staging helper, one-shot initializer, renderer, validators, deployment smoke, release notes, upgrade/rollback guidance, and asset assembly |
| Automation and security | Six server and three firmware GitHub workflow files, immutable action pins, dependency and secret gates, release/stable gates, contract generation, OpenAPI, evidence validation, and the PowerShell audit runner |
| Shared contracts | `pm-protocol/1.0.0` schemas, HMAC vectors, generated OpenAPI, and server/firmware compatibility checks |
| Firmware | All 129 inventoried entries in the independent ESP32-S3 repository: components, main tasks, host/fault/contract/HIL tests, release metadata, tools, workflow files, and operator/security documentation |
| Documentation | Architecture, security, authentication, installation, deployment, testing, migration, firmware, bill-rate, SCE, release, operations, traceability, and audit documentation |

All inventoried first-party text files were readable and included in manual or
automated inspection. Critical and changed paths received direct line review.
Tracked PNG snapshots were inspected as images and by Git object identity rather
than as text. Generated OpenAPI and lockfiles were reviewed as generated
contracts and dependency inputs; they were not treated as hand-authored
application logic.

## Deliberately excluded generated or private areas

The following were not line-reviewed as first-party source: `.git`, dependency
installations (`node_modules`, `.venv`, ESP-IDF `managed_components`), build and
bundle output (`dist`, `build*`), caches, coverage, Playwright transient output,
audit-runner output, test databases, uploaded documents, firmware binaries,
NVS/SD dumps, secrets, certificates/private keys, and release-private or
hardware-private evidence. Their generators, manifests, ignore rules, and
security boundaries were inspected. No unredacted customer bill or production
secret was opened or added to the repository.

## Defect register

Status values are **repaired locally**, **guarded/pending integration**, or
**external limitation**. A locally repaired row is not a statement that the
current candidate passed PostgreSQL, Docker, CI, TrueNAS, publication, or
physical-hardware gates.

| ID | Severity | Defect and root cause | Principal files affected | Repair status and tests | Remaining risk |
| --- | --- | --- | --- | --- | --- |
| A-01 | Critical | An optional setting encrypted and persisted an entire bill PDF. Encryption did not remove prohibited identity, usage, account, balance, or payment content. | `backend/app/config.py`, `backend/app/routes/billing.py`, `backend/app/models.py`, migration `20260815_0008`, `.env.example`, bill tests/docs, deployment mounts | Repaired locally. New imports never persist original bytes, encrypted or otherwise; the ORM/database require `encrypted_artifact_path IS NULL`. `test_bill_artifact_storage.py` covers runtime non-retention and database rejection. | A legacy non-null reference makes the upgrade fail closed. Its file requires a separately reviewed operator privacy procedure; the migration does not silently delete it. |
| A-02 | High | Public rc.3 already returned every authorized home scope in the sensor `/devices` response, including before the first device was enrolled. Home discovery was nevertheless coupled to a sensor-list endpoint and its `sensors.view` permission, while other pages independently chose an unordered first home; there was no permission-independent active-home authority for the whole browser. | `backend/app/routes/settings.py`, API schemas/OpenAPI, `frontend/src/home/*`, `App.tsx`, `SettingsPage.tsx`, API client/tests | Repaired locally. Authenticated `/api/v1/home-scopes` now derives directly from `user_home_scopes` without requiring sensor or billing permissions. One scope auto-selects, multiple require an explicit internal-ID selection, and zero remains safely disabled. Normal labels show only the Home name; duplicate names use `(1)`, `(2)` ordinal labels without exposing UUIDs. Backend and browser tests cover zero/one/multiple scopes and first-sensor enrollment. | Physical enrollment with the marked sensor and the next published server/firmware candidate remains unproved. |
| A-03 | High | Home-specific API routes used unordered first-home selection or actor-wide sets. Dashboard timezone/rate/cost, History, billing, bill drafts, sensors, circuits, and utility settings could be associated with the wrong authorized home. | `backend/app/routes/dashboard.py`, `billing.py`, `settings.py`, API schemas/OpenAPI, frontend API/pages/home context | Repaired locally. Exact `home_id` is used end to end; omission is allowed only for exactly one scope, multi-home ambiguity is 422, and unknown/out-of-scope IDs are indistinguishable 404s. `test_home_selection.py`, authorization tests, unit tests, and `home-scope.spec.ts` exercise isolation. | Local integration passed; remote CI and production multi-home evidence remain pending. |
| A-04 | High | React Query retained protected results across logout/login and briefly retained the old home's results during a scope change. | `frontend/src/auth/AuthScreen.tsx`, `frontend/src/layout/AppShell.tsx`, `frontend/src/home/*`, auth/home-scope tests | Repaired locally. Every non-session query is removed at logout and successful login; a new home renders loading/empty state before its exact results arrive. | Browser evidence is mocked Chromium, not a multi-user production browser session. |
| A-05 | High | Application checks did not create a database-wide mutual-exclusion boundary between immutable raw readings and authenticated permanent-loss ranges; overlapping loss ranges, mutation/deletion of accepted loss evidence, and impossible sample completeness could enter through another writer. | `backend/app/schemas/device.py`, `services/ingestion.py`, `models.py`, migrations `20260815_0008` and `20260815_0010`, ingestion tests | Repaired locally. Migration 0008 locks and preflights the evidence tables, tightens completeness, and adds per-device overlap guards. Migration 0010 rejects UPDATE/DELETE of permanent-loss evidence while preserving INSERT and the frozen raw-reading immutability prerequisite. A real PostgreSQL 17 chain through current head 0012, downgrade/re-upgrade, and 25/25 focused settings/home/bill/SCE/ingestion-guard tests passed; portable migration/guard coverage also passed with PostgreSQL-only cases explicitly skipped outside PostgreSQL. | Existing conflicts intentionally stop migration without rewriting evidence. Target deployment remains pending. |
| A-06 | Medium | Official-source validators could advance before durable parsing, and shared TOU navigation on the public SCE tiered page was mistaken for the primary plan, causing the false `HOLIDAY_RULE_MISSING`; rate-source candidates also lacked one exact-home operational workflow with manual fallback, separated review/publish/activate/reject transitions, overlap control, and honest per-home status. | `backend/app/services/rate_sync.py`, `sce_rate_parser.py`, `rate_workflow.py`, rate routes/models/schemas, migrations `20260815_0009` and `20260815_0011`, worker jobs, backend/frontend tests | Repaired locally. Strong DOMESTIC/Tiered Rate Plan plus Tier 1/Tier 2 evidence now takes precedence over incidental navigation, yielding `seasonal_tiered` with holiday treatment `not_applicable`. Validators advance only after durable usable evidence; Linux fsyncs the parent directory; a stranded 304 gets one bounded unconditional recovery fetch. The database-backed workflow retains exact-home manual idempotency, immutable provenance, legal transitions, serialized versions, and non-overlapping assignments. | The bounded live SCE probe parsed successfully and adversarial regression coverage passed. Displayed public-page rates are rounded evidence; review remains mandatory before publish/activate. |
| A-07 | Medium | Untrusted decimal JSON could admit non-finite values in electrical measurement fields. | `backend/app/schemas/device.py`, ingestion tests | Repaired locally with finite-decimal validation and malformed-payload tests. | Firmware/HIL validation still must prove actual PZEM corruption and edge cases on the marked unit. |
| A-08 | Medium | Mobile sign-out was hidden; nested dialog Escape/focus behavior was ambiguous; narrow dialogs and long content could overflow; charts lacked complete text metadata and sufficiently usable brush handles. | `frontend/src/components/ui.tsx`, `AppShell.tsx`, pages, `styles.css`, frontend unit/E2E snapshots/tests | Repaired locally. The shared dialog stack traps/restores focus, only the top dialog handles Escape, mobile sign-out is reachable, long content is constrained, chart summaries are accessible, and brush handles are at least 24 px in automated geometry checks. | Automated axe and layout checks used Chromium. Firefox, WebKit, real screen readers, zoom/high-contrast, and real touch hardware were not exercised. |
| A-09 | High | Immutable rc.3 normal installation required privileged TrueNAS shell preparation, contrary to the requested UI/SMB workflow. | `deploy/truenas/*`, production YAML, API image, renderer/validators, release workflow/smoke, deployment tests/docs | Repaired in source; external validation pending. The next candidate uses nine UI-created POSIX datasets, an exact 13-file temporary authenticated SMB stage, a network-isolated one-shot initializer, then migration and six long-running services. No normal install step requires SSH, shell, or a container console. | No current target-TrueNAS clean install/upgrade/restart/restore execution exists. Source YAML contains deliberate `UNPUBLISHED_*` image sentinels and is not installable. |
| A-10 | High | The first audit runner inherited arbitrary `PM_*` endpoints and Docker context and started a disposable migration flow without an explicit integration choice. That could target a live database or remote Docker daemon. | `scripts/Invoke-PowerMeterFullAudit.ps1`, runner tests | Repaired and exercised locally. The runner removes inherited `PM_*`, supplies runner-owned test values, proves a local Docker named pipe/socket, and requires `-RunDisposableIntegration` before image builds, Compose startup, or migration. Default execution is read-only except explicit `-ApplySafeFixes`. Retained strict run `20260815T123259Z-f983565e` passed the explicit disposable path and removed its exact resources. | The retained run is local evidence; remote CI and target deployment safety remain separate gates. |
| A-11 | Medium | Release smoke pre-created initializer-owned subdirectories and used certificates shorter than the initializer's seven-day horizon, so it could neither prove first-run ownership repair nor pass its own TLS policy. The Windows stager could also leave files moved by a failed invocation. | `scripts/release_deployment_smoke.sh`, `initialize_host.py`, `Stage-PowerMeterTrueNAS.ps1`, deployment tests | Repaired in source. Smoke creates only dataset roots; initializer creates children. Test certificates use 30 days. Leaf and full chain are checked now and seven days ahead. Stager failure removes only files moved by that invocation. The disposable application stack also completed locally. | Full release-specific smoke and real SMB/TrueNAS execution are pending. |
| A-12 | Medium | Local Compose parsed an unquoted flow-style `tmpfs` value incorrectly, and production log subdirectories did not align cleanly with image/runtime ownership. | `compose.yaml`, `compose.dev.yaml`, gateway/API Dockerfiles, Caddy config, deployment tests | Repaired and exercised locally. Both local and TrueNAS Compose configurations validated; backend, frontend, gateway, and backup completed cache-only builds; the disposable API, database, PDF sandbox, worker, and frontend became healthy before exact cleanup. | Tagged candidate images/YAML and target TrueNAS runtime remain pending. |
| A-13 | Medium | Current workflows did not format/type-check the new Linux initializer or parse the Windows stager; the secret-scan action was on its older Node runtime generation. | `.github/workflows/*.yml`, `pyproject.toml`, workflow/dependency tests | Repaired in source. CI adds initializer Ruff/format/Linux-mypy, PowerShell parse, asset comparison, and immutable gitleaks v3 pin; PyYAML moves from 6.0.2 to 6.0.3. Local dependency and strict scan/diff-integrity gates passed. | No GitHub Actions run exists for the uncommitted repair branch; remote CodeQL, container scanning, SBOM, provenance, and release gates remain pending. |
| A-14 | External | Firmware simulation and prior prerelease evidence cannot certify the actual marked ESP32-S3/PZEM/SD unit. | Nested firmware tests, HIL schema, `.gitattributes`, firmware/release docs | No runtime protocol change was made; `pm-protocol/1.0.0` remains fixed. Added LF text normalization and corrected the documented host-test count from 55 to 59. | Marked-unit identity, electrical, outage, TLS/HMAC, SD/power-loss, OTA rollback, USB recovery, and continuous 72-hour soak evidence are absent; stable promotion remains blocked. |

## Cross-cutting invariant review

- Authenticated PZEM-004T evidence remains the sole source for live values,
  History, intervals, rollups, forecasts, completeness, energy, and usage-based
  cost. A measured zero remains zero and an absent value remains null.
- Bills and SCE documents supply reusable rate facts only. No bill usage,
  readings, totals, balances, payments, address, account, meter identifier, or
  identity is modeled, logged, returned, or used in cost. Original bill bytes
  never enter persistent storage, even encrypted.
- Raw readings remain immutable and unique by `(device_id, sequence)`.
  Permanent-loss evidence cannot overlap a reading or another loss range for
  the same device.
- Money remains Decimal/NUMERIC, authoritative timestamps remain UTC, and SCE
  schedules evaluate in the selected home's configured IANA timezone.
- One-CT devices remain `energy_only`; no whole-home sum or solar export is
  inferred.
- Browser traffic still terminates at the central server. Firmware remains
  outbound HTTPS only with strict TLS, HMAC replay protection, and no sensor
  web server.
- CSRF, sessions, permission checks, SSRF restrictions, upload limits,
  fail-closed PDF isolation, secret separation, and TLS hostname/chain checks
  remain enabled. No `latest` image, TLS bypass, relay, MQTT, remote shell, or
  third-party telemetry was added.

## Validation status at report creation

The retained strict local report at
`artifacts/audit/20260815T123259Z-f983565e/FULL_AUDIT_REPORT.md` inventoried 396
first-party tracked files. Its portable Python suite passed 219 tests with 15
intentional environment/tool-specific skips. Frontend lint, strict TypeScript,
production build, all 31/31 current Vitest tests, and all 36/36 Chromium
Playwright/axe tests passed. Both local and TrueNAS Compose configurations
validated, and backend, frontend, gateway, and backup completed cache-only image
builds. The disposable API, database, PDF sandbox, worker, and frontend became
healthy; the runner then removed its exact containers, networks, volumes, and
local image tags. The current firmware ESP-IDF build passed. Required local
scans and ending Git diff integrity also passed, with warning-classified pattern
findings retained in that report.

Separately, the full backend suite on real PostgreSQL 17 passed 135 tests with 3
expected environment skips. The isolated clean upgrade through
`20260815_0011`, its downgrade to 0010 and return to head, and all 20
rate-workflow concurrency/direct-SQL tests passed. Ruff lint/format passed and
mypy reported no issues in 81 source files. The browser suite covers all
required viewport sizes; manual in-app Chromium checks were performed at
representative 320x568 and 1440x900 sizes with no console error observed.
`validate_release.py`, a dummy rc.4 render plus Compose config/artifact verify,
PowerShell AST parsing, actionlint, and Bash syntax were also green. These are
subsystem results, not one combined whole-repository test count. Historical
rc.3 CI/release/backup evidence remains valid only for rc.3.

At the audit snapshot, the following gates were deliberately **not** marked
passed here: the tagged
public-rc.3 forward upgrade; remote GitHub CI/security/release workflows; signed
rc.4 image and installable-YAML publication; target TrueNAS/ZFS execution;
release-specific rollback restore; complete live SCE evidence; Firefox, WebKit,
and real assistive-technology coverage; and marked-unit hardware-in-loop
certification. The local disposable run is not full signed-release smoke. See
`VALIDATION_MATRIX.md` and `KNOWN_LIMITATIONS.md`.

## Post-audit rc.4 outcome and rc.5 recovery boundary

The audited server branch was merged, and the valid signed server
`v0.1.0-rc.4` tag remains immutable. Tagged workflow run
[`31893354667`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31893354667)
passed the workflow's named `Mandatory release gates` job, published all four
multi-architecture images, and
passed anonymous GHCR access. Its digest-pinned deployment smoke failed
deterministically when `docker compose start` traversed dependencies and
restarted the completed initializer. Release assembly was skipped; rc.4 has no
server GitHub Release or generated YAML and is not an installation authority.
Public server rc.3 remains the install authority and migration predecessor.

The rc.5 recovery source changes no application behavior, protocol, or
migration. It restarts only the six exact captured runtime container IDs,
proves each ID and health state remain stable, and preserves the initializer's
ID, successful exit, and completion time. A fixed allowlisted assertion ID is
added to redacted failure evidence. Runtime/package/image identities advance to
rc.5, and the official generator produces OpenAPI SHA-256
`66b4e1cfb0f5a5797dadd9a8783ff0b192ca416d1f4264c135a4e380b2b94591`
under unchanged `pm-protocol/1.0.0`; current source now extends the migration
head to `20260815_0012` for scoped settings and typed rate-import evidence.
Public firmware rc.4 is historical; a distinct coordinated firmware rc.5
target remains required before a server rc.5 tag.
