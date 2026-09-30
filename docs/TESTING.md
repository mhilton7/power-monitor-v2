# Testing and evidence

Local server gates from repository root:

```powershell
python -m pip install -e '.[dev]'
python -m ruff check backend/app backend/tests worker tests scripts deploy/truenas/initialize_host.py
python -m ruff format --check backend/app backend/tests worker tests scripts deploy/truenas/initialize_host.py
python -m mypy backend worker
python -m mypy --platform linux deploy/truenas/initialize_host.py
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
  --version 0.1.0-rc.8 --revision 0123456789abcdef0123456789abcdef01234567 `
  --api-digest sha256:<published-api-digest> `
  --frontend-digest sha256:<published-frontend-digest> `
  --gateway-digest sha256:<published-gateway-digest> `
  --backup-digest sha256:<published-backup-digest>
docker compose -f release/power-monitor-v2-test.yaml config --quiet
python scripts/verify_release_artifacts.py --manifest release/release-manifest-test.json
```

The rc.8 value above is a local render example, not a tag or
publication claim. The checked-in template cannot become installable until a
new coordinated release supplies real registry digests and passes every gate.

Release workflow gates cover backend unit/integration and PostgreSQL migration
tests; frontend checks; shared protocol vectors; Compose/hardening checks;
dependency/secret/CodeQL/container scans; SBOM/provenance; a clean digest-pinned
deployment with TLS, SSE, upload limits, restarts, encrypted backup, and actual
isolated restore; and firmware release compatibility. The migration gate uses
authenticated GitHub release metadata to select the most recently published
non-draft same-major public V2 Release other than the current tag. It then
requires that exact signed tag in the checkout and requires its semantic
version to be older before exercising the forward path from that release's
database to the current release. Publication-date ordering that selects a
same-major version which is not older fails closed; the gate does not silently
fall through to a different release. Failed tags without a public Release are
not candidates. It exercises only the forward path and does not run prior
binaries against the
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
cleanup. Its failure report includes one fixed allowlisted `failed_assertion`
identifier; arbitrary assertion or log text is rejected. The event stream is
bounded and contains only known service, UTC timestamp, and event enums—never
raw log text. The runtime-recovery check restarts the six exact captured
container IDs directly, avoiding Compose dependency traversal, and proves the
initializer identity and completion time remain unchanged. Those diagnostics
never make a failed smoke eligible for release assembly.

The target TrueNAS deployment suite records machine-readable evidence for clean
migration, service health, HTTPS routing and headers, SSE proxying, upload
rejection, dataset permissions, backup/restore, every individual service
restart, full-stack restart, and persistence. A rollback result additionally
requires an explicit release-specific test that restores or clones the matching
pre-upgrade database into a validated recovery target before binding prior
digests. Target-TrueNAS execution remains separate physical deployment evidence
and cannot be inferred from a GitHub-hosted Docker runner.

Firmware host/fault/simulation/HIL tests live in the independent firmware repository. Hardware certification requires actual marked ESP32-S3/PZEM evidence for readings; proof that the stateless image never mounts or accesses an inserted microSD card and never persists telemetry in NVS; AP/server/DNS/TLS outages; physical cycles; USB recovery; OTA/rollback; bounded memory; and a 72-hour soak. A passing simulator cannot set hardware status to passed.

Every evidence report records schema, version, full revision, UTC generation time, outcome, exact command/environment, input/output checksums, test counts, failures/skips, and physical/simulated classification. A release gate must not be reported passed from missing, stale, or unparsable evidence.

## Historical rc.4 tagged outcome

Signed server tag `v0.1.0-rc.4` and release workflow run
[`31893354667`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31893354667)
are immutable historical evidence. The workflow's named
`Mandatory release gates` job passed; all four
multi-architecture images were published; and anonymous GHCR access passed.
Deployment smoke then failed deterministically because `docker compose start`
restarted the completed initializer dependency and changed its completion time.
All runtime services returned healthy, but the invariant correctly failed.
Assembly was skipped, so there is no server rc.4 GitHub Release or generated
YAML and rc.4 is not installable. The rc.5 source repaired this check and its
tagged workflow later published the complete public, installable server rc.5
asset set.

## Opt-in live official-SCE smoke test

The deterministic catalog and tariff suites use only checked-in, sanitized
official-source fixtures. They never require the live SCE website. An explicit
operator-only smoke test can verify that the current public residential catalog
root remains fetchable and yields bounded crawl links:

```powershell
$env:PM_RUN_LIVE_SCE_SMOKE = '1'
.\.venv\Scripts\python.exe -m pytest -q `
  backend\tests\test_sce_catalog.py::test_live_public_sce_catalog_root_is_fetchable_and_discoverable
Remove-Item Env:PM_RUN_LIVE_SCE_SMOKE
```

This smoke reads only public official SCE pages. It does not log into an SCE
account, write catalog rows, publish a rate, or replace the captured-fixture
closure tests.

## Historical audit-candidate local evidence

On 2026-08-15, the earlier full backend suite on real PostgreSQL 17 passed 135
tests with 3 expected environment skips through revision 0011. The current
remediation then upgraded a fresh pinned PostgreSQL 17.10 database cleanly to
`20260815_0012`, downgraded to 0011, proved the case-insensitive duplicate-email
preflight fails transactionally at 0011, and returned to head. Its 25/25 focused
PostgreSQL settings, home, bill, SCE, and ingestion-guard tests passed. Earlier,
all 20 rate-workflow concurrency/direct-SQL tests passed. That workflow
evidence covers database-backed exact-home manual idempotency, shared serialized
bill/SCE plan-version allocation, non-overlapping assignments with equal-start
rejection, immutable candidate provenance, and the only legal review paths:
`reviewed -> published -> activated` or `reviewed -> rejected`. Ruff
lint/format passed and mypy reported no issues in 81 source files.

Frontend lint, strict TypeScript, production build, 31/31 Vitest tests, and
36/36 Chromium Playwright tests passed. No whole-repository aggregate count is
claimed. These are local candidate results, not tagged CI, target TrueNAS,
publication, or physical hardware evidence.

## Historical pre-rc.3 local validation snapshot

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

- Signed public server/firmware rc.1, rc.3, rc.5, and rc.6 releases, plus signed
  public firmware rc.2 and rc.4, are historical evidence. The signed server rc.2 tag's run
  `31866197054` failed the cross-repository OpenAPI-hash check before server
  images or release assets were published.
- Public rc.1/rc.3/rc.5/rc.6 GHCR digests, attestations, generated TrueNAS YAML, and
  release smokes are version-specific historical evidence. There is no server
  rc.2 image set or YAML. Server rc.4 published images but no Release or YAML
  after deployment smoke failed; those images are not an installation
  authority. Public server rc.6 remains installable with its attached assets.
  Hardware execution confirms firmware rc.1 through rc.5 crash in the main
  stack before provisioning. The rc.8 coordinated source remains
  unpublished and the checked-in template correctly retains `UNPUBLISHED_*`
  sentinels pending its own coordinated tag.
- The target-TrueNAS clean install, forward upgrade, restored rollback,
  restart, and permission suite has not run for the post-rc.3 initializer model.
- No marked-unit PZEM/ESP32-S3/SD identity, electrical, TLS/HMAC, OTA rollback,
  physical-cycle, USB-recovery, or continuous 72-hour soak evidence exists.
  Simulation cannot satisfy those gates, so stable promotion remains blocked.

## 2026-09-29 local live-pricing and chart repair

### Baseline and scope

The authoritative checkout was `E:\Documents\ChatGPT\PowerMonitorV2`, remote
`https://github.com/mhilton7/power-monitor-v2.git`, initially clean on `main` at
`ed829f4fda95981e489a88db3cfdc7dd9c7b8446` (the RC30 range-slider merge). Work is
local on `codex/repair-live-pricing-chart-performance`. The application version
remains `0.1.0-rc.30`; this repair has not created a new release identity. Local
tools are Node.js `v26.0.0`, Python `3.13.14`, and the existing Recharts `3.10.1`
frontend dependencies. No dependency upgrade was needed.

The supplied review instead described `mhilton7/power-monitor` at
`df581522266227b0258c3303b551a7f6ec2e5362`. Its Chart.js/Canvas architecture,
adapters, paginated History loader, and trailing 750 ms event handler do not
describe this checkout. Findings were revalidated against this application's
Recharts pages, API schemas, server tariff engine, and existing range-selection
regressions; no application code or architecture was transplanted. No deployed
TrueNAS build was inspected or changed by this repair.

### Confirmed causes and targeted changes

| Area | Reproduced issue and repair |
| --- | --- |
| Current pricing | Home did not present the applicable server-backed marginal price and scheduled transition. The previous dashboard calculation could use the selected sensor's usage as the account tier basis. Current pricing now resolves the selected home's unique account, effective published assignment, account billing cycle, and explicitly verified billing-source evidence through the existing decimal rate engine. |
| Live estimate | A stale measurement could retain a cost/hour estimate. Pricing now requires fresh authenticated load from every expected selected aggregate member; stale, missing, revoked, incomplete, denied, and failed states do not become zero-valued live estimates. Known flat/TOU prices remain available without fresh load. |
| Tariff boundaries | Regressions cover exact marginal tier boundaries, clock-only TOU changes, scheduled assignment changes, DST, cycle rollover, equivalent adjacent TOU segments, and seasonal/calendar transitions beyond a weekly look-ahead. A seasonal reproduction returned October 15 instead of the actual October 1 change before repair. Usage-dependent future tier crossings are not assigned invented clock times or guaranteed future prices. |
| Usage uncertainty | Estimated usage spanning a baseline-credit boundary could resolve the same tariff period but different effective prices. Both bounded effective prices must now agree. Mutable daily settings cannot replace unresolved immutable account-tier evidence. Incomplete hybrid usage retains a determinable TOU label without guessing its tier or price. |
| Request amplification | The server emits a generic `refresh` every five seconds. The old handler invalidated History and Billing on every such message, while Home's changing `generated_at` created additional History keys. Live-number refreshes are now separate from bounded History recovery; one semantic cache key retains each Home snapshot and its exact fetched window. |
| Scheduling and recovery | Measurement bursts previously issued immediate repeated invalidations, and an SSE error permanently closed the stream. The scheduler coalesces bursts with a three-second maximum wait, preserves a dirty follow-up during slow requests, avoids cancelling valid in-flight refreshes, retains native SSE reconnection, and recovers on foreground/online events. A thirty-second recovery timer, plus the coalescing delay, refreshes History without relying on measurement payloads the current server does not emit. |
| Plot lifecycle | Home and History did not consume request AbortSignals. Obsolete selections are now cancelled, late results cannot overwrite a newer selection, and valid same-scope plots remain mounted during refreshes and failures. Refresh/stale notices reserve their layout footprint. Hidden optional Home charts do not fetch their unused History series. |

The event stream supplies no trustworthy dirty-range/revision payload. Accepted
measurement notifications therefore conservatively dirty all active History
queries, including backfilled/older intervals; the implementation does not discard
an event merely because its timestamp is older. It does not claim a new delta API
or incremental database cache. Existing bucket resolutions, complete source
results, gap breaks, totals, exports, and the timestamp-based range controller
remain intact. Animations were already disabled. No point decimation, curve
replacement, chart-library change, or speculative resize-owner rewrite was made.
The original brief's pagination-chain and duplicate Chart.js resize hypotheses
were not applicable to this server/frontend pair.

### Implementation traceability

| Files | Purpose |
| --- | --- |
| `backend/app/services/live_pricing.py`, `billing_usage.py`; `backend/app/routes/dashboard.py` | Consistent account-aware pricing, bounded calendar evaluation, aggregate usage SQL, freshness enforcement, and additive `/api/v1/home/pricing` response independent of expensive historical summaries. |
| `backend/app/services/cost_engine.py`; `backend/app/routes/billing.py` | Expose the existing engine's incremental marginal-period/tier-bound semantics and share the existing short-gap estimator. Historical interval pricing is not replaced with current-price multiplication; the extracted estimator retains its prior rules and no-History-write boundary. |
| `frontend/src/components/LivePricing.tsx`; `frontend/src/api/index.ts`, `schemas.ts`; `frontend/src/pages/HomePage.tsx`, `HomePage.css` | Parse and display one pricing response with both tier/TOU context, exact server-calculated money, account-local transition times, explicit availability states, and independent deadline refreshes. Home selection is checked against the response identity. |
| `frontend/src/hooks/useLiveUpdates.ts`, `frontend/src/lib/refreshScheduler.ts`; `frontend/src/pages/HomePage.tsx`, `HistoryPage.tsx` | Separate live/history scheduling, cancellation, stable scoped snapshots, retained plots, hidden-card gating, and snapshot-consistent History export/time labels. |
| `backend/tests/test_live_pricing.py`, `test_live_pricing_review.py`, `test_live_pricing_work.py`, `test_home_selection.py`; frontend pricing, scheduler, Home, History, and live-update tests | Synthetic tariff/API arithmetic, permissions/authority, null-versus-zero, freshness, effective dates, uncertainty, request counts, slow responses, cancellation, backfill, recovery, and range preservation. |
| `frontend/tests/e2e/live-pricing.spec.ts`, `home.spec.ts`, `chart-performance.spec.ts`, `chart-performance-server.ts`, `chart-performance.config.ts`; `frontend/playwright.config.ts`, `tests/e2e/mocks.ts`, `tests/fixtures.ts`, `tests/fixtures/live-pricing-api.json` | Production-build browser scenarios, controlled normal-route fixtures, clock-only transition checks, repeatable performance capture, and cross-browser pricing coverage alongside existing range tests. The local-drag test uses a connected stream; a separate native reconnect test verifies recovery without changing the selected timestamps. |
| `shared/openapi/power-meter-v2.openapi.json`; `tests/test_dependency_lock.py` | Regenerated additive server endpoint contract and its exact SHA-256 assertion (`7559ac0e4418af2c64f5e7d560ea53424f4b5fa4e66f8c0a4603a62036aacda6`). The shared protocol remains `pm-protocol/1.0.0`; the checksum check was updated, not removed. |

### Pricing evidence and focused verification checkpoints

The checked-in [sanitized API fixture](../frontend/tests/fixtures/live-pricing-api.json)
is verified against actual `/api/v1/home/pricing` serialization, with synthetic
identifiers and tariffs. It identifies the account, assignment, immutable version,
UTC evaluation time, `America/Los_Angeles` billing cycle, verified usage source,
and load freshness deadline. It contains no customer bill, account identity, or
production readings. Frontend tests validate and render that response, rather
than testing only independently fabricated UI values.

The synthetic tariff's Tier 1 ends at 10 kWh at $0.20/kWh; Tier 2 is $0.30/kWh.
Starting at 9.5 kWh, a new 1 kWh interval costs exactly
`0.5 × $0.20 + 0.5 × $0.30 = $0.25`. The resulting 10.5 kWh cycle usage has a
$0.30/kWh marginal price, and fresh 2,000 W load yields `$0.60/hour`. Coverage
percentage is not tier progress. Fixed charges/taxes remain separate from this
marginal estimate; historical costs remain chronological engine calculations.
At an inclusive threshold, the existing Billing Cycle summary can classify the
completed usage as Tier 1 while the new current marginal pricing correctly
identifies the next increment as Tier 2. These are distinct billing quantities,
not a retroactive repricing of the completed Tier 1 energy.

These are intermediate local checkpoints, not a claim that every release gate
or target environment passed:

| Check | Observed result and evidence |
| --- | --- |
| Existing pricing baseline | 30 passed, 0 failures; `.test-runtime/pricing-baseline.xml`. |
| New initial pricing reproductions | 3 failures before repair, then 3 passed; `.test-runtime/pricing-reproductions-before.xml` and `pricing-reproductions-after.xml`. |
| New chart scheduling reproductions | The initial live-update run reproduced 3 failures: heartbeat History/Billing invalidation, twenty immediate History invalidations from a twenty-event burst, and permanent SSE closure. |
| Focused frontend repair suite | `npm.cmd test -- tests/live-updates.test.tsx tests/refresh-scheduler.test.ts tests/history.test.tsx tests/home.test.tsx`: 4 files, 33 tests passed in 10.02 seconds. |
| Frontend static checks | `npm.cmd run lint` and `npm.cmd run typecheck` passed. These checks alone do not establish chart smoothness. |
| Focused backend and existing engine/selection/quality regressions | 67 passed, 0 failures/errors in 72.239 seconds; `.test-runtime/pricing-verified-final.xml`. |
| Independent pricing review regressions | 5 passed, 0 failures/errors in 6.731 seconds; `.test-runtime/pricing-review-final.xml`. The seasonal next-change test was first observed failing with the incorrect October 15 date. |
| Sparse future event boundary | The intraday event-calendar reproduction failed before repair; the subsequent six-test review run passed, `.test-runtime/pricing-event-before.xml` and `pricing-event-after.xml`. |
| Real-route work check | `test_live_pricing_work.py` exercises 1, 8, and 32 sensors with 288 accepted five-minute intervals per sensor, three warmed requests per route. Its assertions bound assignment queries and prohibit interval-cost queries/full interval materialization in the pricing-only endpoint. Three cases passed; `.test-runtime/pricing-real-route-final-work.xml`. SQLite timings are not production PostgreSQL latency evidence. |

The focused review was also exercised with this isolated-database command;
the JUnit files above record subsequent verification checkpoints:

```powershell
$env:PM_DATABASE_URL = 'sqlite+aiosqlite:///.test-runtime/live-pricing-review-tests.sqlite3'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_live_pricing_review.py -q
.\.venv\Scripts\python.exe -m ruff check backend/tests/test_live_pricing_review.py
.\.venv\Scripts\python.exe -m ruff format --check backend/tests/test_live_pricing_review.py
```

All database fixtures are disposable and explicitly separate from deployment
data. Browser fixture timings use normal application routes but synthetic local
responses; they cannot prove production network/database latency or physical
phone behavior. A server-relative pricing deadline is still subject to response
transit time, browser suspension, and failed connections. A physical iPhone/Safari
or Android session and the deployed TrueNAS application are not certified by
local browser emulation, tests, or screenshots.

### Preserved boundaries and rollback

No migration, database reset, production configuration change, dependency
upgrade, firmware change, push, publication, or deployment is part of this
repair. Existing navigation, authenticated sensor communications, raw readings,
ingestion/backfill, rate-source and bill-import workflows, permissions, session
controls, backups, exports, and TrueNAS ports/volumes/secrets are retained. Bill
documents remain rate-source evidence only. No retrospective cost rewrite or
physical hardware certification is claimed.

Rollback requires no schema or data conversion. First preserve the reviewed
tracked-file patch and the new/untracked repair files separately, together with
any later user changes. Build and verify baseline `ed829f4` in a separate checkout
without altering this working tree, or apply a reviewed reverse patch containing
only this repair's hunks and new files. Do not discard unrelated work or reset a
database. If a later, separately authorized release is deployed, use the existing
release-specific, digest-pinned deployment/rollback workflow with its matching
data-compatibility checks; keep TrueNAS datasets, secrets, and network bindings
unchanged. This local repair has not performed that deployment or rollback.

### Native production-build chart performance evidence

The final measurements used the preserved regular production baseline build from
`ed829f4` and the repaired regular production build, not a development or React
profiling build. Environment: Windows `10.0.26200`, 32 logical AMD processors,
Node `26.0.0`, Chromium `151.0.7922.34`, one Playwright worker, fresh browser
contexts, disabled browser HTTP cache, and an 80 ms synthetic History-response
delay. Desktop was 1440×1000 at 1× CPU; the mobile test profile was a 390×844
responsive viewport at 4× CPU with synthetic touch PointerEvents. This is not a
physical phone, mobile GPU, mobile browser user agent, or field INP measurement.

Only `Date` was shifted to an advancing synthetic August 13 calendar. Native
timers, `performance`, animation frames, and long-task observation were left
unchanged. Preliminary runs using the existing mock helper's Playwright fixed
clock were explicitly rejected as native-frame timing evidence, because that
clock also wraps timing APIs. Only `baseline-native` and `after-native` results
below are authoritative for this comparison.

Fixtures exercised the normal Home and History routes with 1, 8, and 32 sensors,
an explicitly configured non-overlapping service branch, measured zeros, peaks,
and null gaps. Home fetched 288 five-minute points per series; History exercised
Today, 7 days, 30 days, and Billing cycle (up to 720 hourly points). Each primary
scenario sent three heartbeats, four generic refresh events five seconds apart,
and an accepted-reading event; moved the existing slider; and checked that a
manual selection survived refresh and viewport rotation. These are synthetic
HTTP response fixtures, not production database/network or sensor evidence.

| Sensors / profile | Home first-curve ready, ms before → after | Slider feedback p95, ms before → after | History requests before → after | History JSON kB before → after | Home-phase long tasks before → after |
| --- | --- | --- | --- | --- | --- |
| 1 / desktop | 952 → 945 | 34.2 → 34.3 | 39 → 10 | 1103.9 → 303.4 | 0 → 0 |
| 8 / desktop | 900 → 901 | 34.7 → 34.5 | 40 → 9 | 1134.1 → 288.1 | 1 → 1 |
| 32 / desktop | 919 → 927 | 34.7 → 34.1 | 39 → 9 | 1118.1 → 290.7 | 1 → 1 |
| 1 / mobile 4× | 1212 → 1538 | 35.1 → 41.4 | 39 → 10 | 1063.8 → 314.5 | 20 → 13 |
| 8 / mobile 4× | 1405 → 1402 | 34.6 → 34.4 | 39 → 10 | 1066.8 → 315.3 | 22 → 13 |
| 32 / mobile 4× | 2032 → 2005 | 51.0 → 47.1 | 39 → 9 | 1077.9 → 290.7 | 22 → 14 |

Request/byte columns cover History throughout each primary scenario, including
the subsequent History presets. Bytes are uncompressed fulfilled JSON bodies,
not compressed wire traffic. Input timing is a local proxy from a pointer event
through changed slider DOM and two native animation frames, approximately forty
samples per scenario. It does not turn every filter request into a sub-200 ms
operation. Long-task columns include initial Home rendering, live refreshes,
range interactions, screenshots, and rotation; they are not drag-only counts.

All six profiles reduced heartbeat-induced History calls from 12 to zero and
the four routine five-second refresh events' History calls from 16 to zero.
Home requests remained 10 in every primary scenario: live-number updating was
not disabled. Mobile SVG mutation counts fell from 570/574/570 to 354/350/350
for 1/8/32 sensors. Observed resize callbacks remained eight on mobile, providing
no evidence for a resize storm. The 32-sensor mobile frame trace retained a
16.8 ms frame-interval p95; intervals over 50 ms fell from 26 to 18, while the
worst interval remained approximately 400 ms. Initial readiness was not uniformly
faster, and the one-sensor mobile result regressed by 326 ms in this single run.
The established improvement is less redundant retrieval/rendering, not a claim
that already-responsive slider feedback became dramatically faster.

Separate bounded-custom tests used 32 sensors and 2,880 five-minute points over
ten days, preserved real gap breaks, changed sensor and all seven supported
metrics, and committed an actual slider movement. Desktop readiness was
216.8 → 220.8 ms; mobile readiness was 894.5 → 698.5 ms. Three observed committed
range-to-frame samples were 24.7/34.8/30.8 → 24.6/19.8/20.1 ms on desktop and
104.5/92.4/119.4 → 74.9/72.0/90.7 ms on mobile. These three samples are not a
statistically broad p95 claim. Uncached mobile selector round trips still took
approximately 283–349 ms after repair, including the 80 ms fixture delay; cached
power selection took 166 ms. The actual API has a bounded full-result response,
not the other repository's continuation-pagination contract.

The remaining main-thread limitation is explicit: the 32-sensor mobile Home
trace contains 150 ms and 133 ms tasks at keyboard range commits, plus startup,
refresh, and rotation work. The History interaction contains a 54 ms task.
The above-50 ms task target is therefore not fully met. Necessary SVG redraws
remain; no speculative point reduction or styling rewrite was introduced to
hide them. Physical-phone testing, individual React component render counts,
React profiler durations, real network latency, and production PostgreSQL
execution plans are not established by this browser benchmark.

#### Separate React attribution and ten-minute resources

A separate experiment installed the supported hook used by the installed
production React renderer to observe actual `onCommitFiberRoot` notifications.
It did not modify the application or reuse its instrumented run as latency
evidence. For 32 sensors, desktop 1× CPU, and four generic refreshes over twenty
seconds, React `19.2.8` root commits fell from **52 to 35**. All API requests in
that phase fell from **36 to 16**; Home stayed at four, History fell from sixteen
to zero, and the new pricing response was fetched four times. These are root
commit notifications including clock/fetch updates, not individual component
render invocations or profiler durations. Evidence is `react-root-commits.json`
in each native result directory.

The repaired build completed a **600.386-second** foreground soak with 32
sensors and 21 forced-GC samples, alternating internal Home/History navigation
every thirty seconds while emitting live refreshes. Internal navigation retained
the QueryClient; page reloads were not used to hide retained resources. Every
sample had one document and one EventSource, with two charts on Home and one on
History. From five minutes onward the sampled resources were stable: Home had
3,160 DOM nodes and 1,623 listeners; History had 702 nodes and 900 listeners.

Heap did not become perfectly flat: History used 13.61 MiB at startup,
17.39 MiB at five minutes, and 18.06 MiB at ten minutes; Home used 19.18 MiB at
5.5 minutes and 19.78 MiB at 9.5 minutes. Those measurements include the growing,
bounded frame-capture buffer and normal warm-up/cache retention. The result
supports stable observed DOM/listener/chart resources, not an unlimited-session
heap ceiling or a directly measured QueryClient cache-entry count. A longer
uninstrumented session would be needed to establish a long-term memory plateau.
Other functional browser tests ran in separate processes on the same OS host
during this resource soak; no interaction-latency claim is taken from the soak.

#### Repeatable commands and local artifacts

From `frontend`, the valid primary after run was:

```powershell
$env:PM_CHART_BENCHMARK = 'after-native'
$env:PM_BENCH_DIST = '../.test-runtime/chart-performance/final-native-dist'
npx.cmd playwright test --config tests/e2e/chart-performance.config.ts --grep 'chart production benchmark'
```

The corresponding before run used `PM_CHART_BENCHMARK='baseline-native'` and
`PM_BENCH_DIST='../.test-runtime/chart-performance/baseline-dist'`. It ran the
same config without a grep filter: six primary cases passed and the opt-in soak
was skipped. The final after matrix passed six cases in 3.7 minutes. Both
preserved builds were made with `npm.cmd run build`; source changes were frozen
before the final after build. The repaired preserved bundle includes
`HomePage-Ca3_becG.js` and `index-IXCs9zwi.js`.

Additional commands, using those same two run/build environment pairs:

```powershell
# Matched custom range, metrics, and sensor tests: 2 passed before, 2 after.
$env:PM_BENCH_EXTRA = '1'
npx.cmd playwright test --config tests/e2e/chart-performance.config.ts --grep 'bounded custom'
Remove-Item Env:PM_BENCH_EXTRA

# Repaired build only: 1 passed, 10.1 minutes.
$env:PM_BENCH_SOAK = '1'
npx.cmd playwright test --config tests/e2e/chart-performance.config.ts --grep 'ten-minute'
Remove-Item Env:PM_BENCH_SOAK

# Separate attribution: 1 passed before, 1 after, approximately 25 seconds each.
$env:PM_BENCH_REACT = '1'
npx.cmd playwright test --config tests/e2e/chart-performance.config.ts --grep 'separate React'
Remove-Item Env:PM_BENCH_REACT

npx.cmd eslint tests/e2e/chart-performance.spec.ts tests/e2e/chart-performance.config.ts tests/e2e/chart-performance-server.ts tests/e2e/mocks.ts
```

The focused lint command passed. The preserved baseline and final production
builds, JSON results, PNG screenshots, and Playwright traces remain local under
the ignored `.test-runtime/chart-performance/` directory. They were not committed
or published. Native result files are `baseline-native/{1,8,32}-{desktop,mobile}.json`
and their `after-native` counterparts; custom reports are `custom-desktop.json`
and `custom-mobile.json`. The final resource record is
`after-native/ten-minute-soak.json`. Traces are under the phase-specific
`playwright*` subdirectories. Clean chart/pricing locator captures are
`after-native/pricing-{desktop,mobile}.png`,
`after-native/dashboard-chart-{desktop,mobile}.png`, and
`after-native/custom-chart-{desktop,mobile}.png`. Full-page images also exist;
sticky navigation can overlap a scrolled full-page capture. The mobile pricing
crop shows the requested values but its last disclosure line overlaps the
sticky bottom navigation in the capture. These images contain synthetic test
fixtures only.

### Final verification and remaining approval

This local implementation is not a release-gate or production-certification
claim. Final frontend production code matches the measured preserved build.
A later backend-only review reproduced another calendar edge case: a season
starting Saturday July 1 incorrectly predicted August 1 rather than the first
weekday peak on Monday July 3. The schedule now evaluates a deduplicated weekly
window after each explicit calendar boundary. The reproduction failed before
repair; seven calendar/review tests passed afterward. Evidence:
pricing-season-week-before.xml and pricing-season-week-after.xml under
.test-runtime. Final executable backend change: September 29, 18:36:09 PDT.

The final source subsequently passed 66 pricing, Home-selection, cost-engine,
and Billing tests, plus three route-work tests. The larger portable run below
preceded this narrow refinement; it is not represented as a single all-green
run of the final source.

| Check | Exact outcome |
| --- | --- |
| npm.cmd run check, in frontend | Lint, typecheck, 18 Vitest files / 134 tests, and regular production build passed; Vitest duration 32.90 seconds. Log: .test-runtime/live-pricing-chart-frontend-final.log. |
| Final npm.cmd run lint; npm.cmd run typecheck | Both passed again after the final browser evidence tests were added. |
| python -m pytest -q --basetemp=.test-runtime/pytest-live-pricing-chart-20260929-final --junitxml=.test-runtime/live-pricing-chart-python-acceptance.xml | 490 collected: 452 passed, 23 skipped, zero assertion failures, 15 temporary-directory setup errors; JUnit duration 406.232 seconds. Windows sandbox denied pytest-created directories and cleanup, even under the workspace. Exit 1, not a clean pass. |
| Elevated python -m pytest tests/test_full_audit_runner.py tests/test_dependency_lock.py -q --basetemp=.test-runtime/pytest-live-pricing-chart-elevated-20260929 --junitxml=.test-runtime/live-pricing-chart-runner-elevated.xml | 24 passed in 22.70 seconds, including all 15 previously blocked cases; exit 0. Only disposable local test files were used. |
| Final-source focused Python tests | 66 passed in 74.38 seconds, .test-runtime/pricing-final-week-focused.xml; three work checks passed in 8.07 seconds, pricing-final-week-work.xml. |
| Combined Python coverage | 468 distinct test cases passed and 23 were skipped across the broad run and targeted reruns. This is deduplicated cross-run coverage, not a fabricated single-run result. |
| python -m ruff check backend/app backend/tests worker tests scripts deploy/truenas/initialize_host.py | Passed. |
| python -m ruff format --check backend/app backend/tests worker tests scripts deploy/truenas/initialize_host.py | 121 files already formatted; the final nine changed backend files also passed after the calendar refinement. An exploratory broader check flagged three unchanged legacy Alembic files; they were not reformatted. |
| python -m mypy backend worker; python -m mypy --platform linux deploy/truenas/initialize_host.py | Passed: 104 source files and one Linux-host initialization file respectively. |
| python scripts/generate_contracts.py; python scripts/validate_contracts.py | Contract regenerated; eight JSON schema/vector files plus pm-protocol/1.0.0 validated. Only the additive OpenAPI contract changed. |
| python scripts/validate_release.py; docker compose -f deploy/truenas/power-monitor-v2.yaml config --quiet --no-interpolate | Both static checks passed. These do not start containers, verify image execution, or deploy anything. |
| git diff --check | Passed. Dependency manifests/locks, deployment YAML, release workflows, firmware, and migrations remain unchanged. |
| $env:PW_RANGE_CROSS_BROWSER='1'; npm.cmd run test:e2e -- --workers=2 | 68 passed, four visual-baseline failures, 12 intentional skips in 3.9 minutes; exit 1. Log: .test-runtime/live-pricing-chart-playwright-acceptance.log. All executed behavioral checks passed. |

The focused final Python command targeted backend/tests/test_live_pricing.py,
test_live_pricing_review.py, test_home_selection.py, test_cost_engine.py,
test_billing_quality.py, and test_billing_cycle_summary.py. The separate work
command targeted test_live_pricing_work.py. Python commands used the repository
.venv interpreter, PM_REQUIRE_POSTGRES_TESTS=0, and a disposable SQLite URL under
.test-runtime, never the deployment database.

An earlier full run had one stale OpenAPI checksum assertion plus the same
15 sandbox errors: 451 passed, 23 skipped, one failure and 15 errors. The checksum
was updated to the reviewed generated bytes; the assertion remains strict.
Interrupted preliminary Python runs and fixed-clock timing trials are not
counted as acceptance passes.

Browser engines were Playwright 1.62.1's Chromium 151.0.7922.34, Firefox 153.0,
and WebKit 26.5 on Windows. Scheduled-price transitions appeared 331 ms, 560 ms,
and 396 ms after the synthetic boundary respectively, with zero History requests
at that boundary. This meets the two-second foreground target on the intercepted
local API connection, not a server/network latency guarantee.

The first full browser run had 66 passes, five failures and 11 skips. Four were
the visual differences below. The fifth was a test-fixture mismatch: its finite
SSE body closed every three seconds, so restored native reconnection correctly
triggered recovery during a test intended to isolate local dragging. The revised
connected-stream case retains exact zero-History-request, long-task, layout-shift,
and selected-range assertions. A separate native reconnection case verifies
exactly two recovery History requests with unchanged selected timestamps. Both
focused tests passed in 27.4 seconds and passed again in the final suite.
Application behavior was not disabled to satisfy that test.

Four screenshot baselines remain unchanged and failing:

| Reviewed screenshot | Difference | Disposition |
| --- | --- | --- |
| History, 412×915 | 5,558 pixels / 2%; loaded-query wording and reserved refresh-status space | Awaiting permission to replace baseline |
| Multi-sensor Home, 1280×720 | 37,044 pixels / 5%; added pricing area | Awaiting permission to replace baseline |
| Desktop Home, 1440×900 | 71,555 pixels / 6%; added pricing area | Awaiting permission to replace baseline |
| Tablet Home, 768×1024 | 27,976 pixels / 4%; added pricing area | Awaiting permission to replace baseline |

Actual and expected images were visually inspected. Existing header-selector
width drift was also visible; this repair does not modify that header. The
permission guard rejected --update-snapshots as possible regression-test
weakening without direct authorization. The user was asked to authorize only
these four reviewed replacements. No replacement or threshold relaxation was
performed. These four failures remain outstanding, not hidden or called passing.
The 12 skips are ten opt-in performance experiments (executed separately above)
and two existing Chromium-only performance cases on Firefox/WebKit. Local
WebKit teardown also logged one refused request to the absent backend after
interception ended; its behavioral test passed.

Final warmed route-work medians, with 288 intervals per sensor and three samples
per route on disposable SQLite:

| Sensors / intervals | Full Home, ms | Pricing-only, ms | SELECTs: Home / pricing |
| --- | --- | --- | --- |
| 1 / 288 | 51.81 | 22.31 | 50 / 24 |
| 8 / 2,304 | 189.13 | 31.52 | 71 / 38 |
| 32 / 9,216 | 798.50 | 75.58 | 143 / 86 |

Assignment SELECTs remain two at every size. Pricing performs no historical
interval-cost query or full interval-row materialization. Current-cycle energy
still requires a database aggregate scan. These concurrent-host timings compare
two final routes, not before/after production PostgreSQL performance.

The 23 Python skips require unavailable Linux sandbox behavior, PostgreSQL
triggers/concurrency or a live API, opt-in live SCE access, Linux-runner jq, or
symlink privileges. Docker's Linux engine was unavailable, so container/image,
live PostgreSQL, and TrueNAS runtime checks were not run. No migration was
introduced. A future separately authorized release must ship matching backend
and frontend builds for the new endpoint using the existing digest-pinned
deployment workflow. No new service, port, volume, secret, or deployment
configuration is required.

Useful local visual evidence:

- [Desktop pricing](../.test-runtime/chart-performance/after-native/pricing-desktop.png)
- [Mobile pricing](../.test-runtime/chart-performance/after-native/pricing-mobile.png)
- [Desktop dashboard chart](../.test-runtime/chart-performance/after-native/dashboard-chart-desktop.png)
- [Mobile dashboard chart](../.test-runtime/chart-performance/after-native/dashboard-chart-mobile.png)
- [Mobile custom History chart](../.test-runtime/chart-performance/after-native/custom-chart-mobile.png)
- [Final browser report and screenshot differences](../frontend/playwright-report/index.html)

The browser report also includes centered pricing-detail attachments at 1440 px
and 390 px. All visual evidence uses synthetic fixtures. Remaining limitations
are the unapproved visual baselines, above-50 ms SVG commit work, modest observed
heap drift, absent deployed-build access, unavailable container/PostgreSQL gates,
and unperformed physical-phone testing. Nothing was committed, pushed,
published, or deployed.

## RC31 publication preparation — 2026-09-29 (America/Los_Angeles)

The preceding repair report is the pre-publication snapshot, not the current
publication state. The user subsequently approved upload/publication, the
reviewed native screenshot baselines, necessary pinned security/build repairs,
and coordinated metadata-only firmware RC31. This section supersedes the
earlier outstanding-baseline and no-publication statements without relabeling
the original benchmark measurements.

Server/frontend identity is 0.1.0-rc.31; firmware is v0.1.0-rc.31 build 34.
Generated OpenAPI SHA-256 is
c7d2ef230f4e3f183251cd010731a122875cddf24cce829da69dfd8ffb0c3c74.
Control/telemetry protocols, migration head 20260829_0020, production service
layout, volumes, secrets, sensor runtime sources, and deployment gates remain
unchanged. No actual TrueNAS deployment or physical sensor update is performed.

| RC31 preparation check | Actual outcome |
| --- | --- |
| Frontend npm run check | Lint/typecheck/build passed; 134 unit tests in 18 files passed in 25.21 seconds. |
| PW_RANGE_CROSS_BROWSER=1; npm run test:e2e -- --workers=2 | 72 passed, 12 intentional opt-in/engine skips, zero failures in 3.9 minutes. All executed screenshot comparisons are strict. |
| Clock-only price transition | Chromium 337 ms, Firefox 464 ms, WebKit 449 ms; zero History requests at each boundary. Synthetic intercepted connection, not production-network evidence. |
| npm audit --audit-level=high | Exit 0 after undici 8.10.2; two moderate development-only Vitest dependency entries remain. No audit threshold changed. |
| Python release/dependency/tools suite | 73 passed, four environment skips in 5.22 seconds: three Linux jq cases and one Windows symlink privilege case. |
| PDF regression suite on pypdf 6.16.1 | 25 passed in 26.66 seconds. |
| New RC31 dependency regressions | Three failed before the pinned repairs; all three passed afterward. |
| Exact Python runtime-lock audit | 47 packages, zero known vulnerabilities. |
| Gateway Go 1.26.6 checks | Module verification and tidy-diff passed; Linux AMD64 and ARM64 cross-builds passed; repeated AMD64 build was byte-identical. |
| Ruff check / format | Passed; 122 files already formatted. |
| Static release/deployment validator | Passed; unchanged eight-service production template remains fail-closed pending real registry digests. |
| Firmware cross-repository validator | Passed against the exact RC31 OpenAPI and existing device schema/vector bytes. |
| Firmware host/security preparation | 115/115 host tests, 36/36 fault cases, 120-day simulation (10,368,000 samples), 11 PowerShell UX tests, AST/source-policy gates, and live OSV audit (three subjects, zero findings) passed. |

Four native Windows baselines and five native Linux baselines were visually
reviewed and updated for the intended pricing/status changes. Linux actual
images came from PR run 36657927862 and matched byte-for-byte across its first
attempt and both retries; Windows images were regenerated natively. The extra
Linux mobile Home image shows the same approved pricing area. No comparison
tolerance, assertion, permission boundary, or runtime UI behavior was weakened.

Security/build changes are limited to pypdf 6.16.1, undici 8.10.2, gRPC 1.83.2
and its necessary transitive closure, API/backup libuuid 2.41.6-r1, frontend
libexpat 2.8.5-r0 and libuuid 2.42.3-r1, gateway curl/libcurl 8.22.0-r0, and
backup jq 1.8.2-r0/tzdata 2026d-r0. Existing base-image digests, Go/Caddy versions,
frameworks, and security scan policies are unchanged. These address confirmed
PR build/security failures; they are not speculative dependency modernization.

Local logs are under .test-runtime/rc31-frontend-check.log,
.test-runtime/rc31-frontend-browser-acceptance.log,
.test-runtime/rc31-frontend-audit-high.log, and
.test-runtime/rc31-python-lock-audit.json. Fresh Linux/PostgreSQL/container
CI and signed-tag release jobs remain authoritative for publication. Local
Docker is unavailable; no local container pass is claimed. Above-50 ms SVG
commit work, modest observed heap drift, physical-phone testing, target-TrueNAS
recovery, and marked-unit certification remain limitations from the repair
report. The existing verified-asset upgrade and restored-rollback procedures
remain mandatory; no database reset or app-only rollback is authorized.
