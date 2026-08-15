# Repair summary

This summary describes post-rc.3 source repairs. Public `v0.1.0-rc.3` is
immutable and does not contain these changes. The source checkout remains
non-installable until a new coordinated, signed server/firmware candidate
passes its gates and supplies digest-pinned release YAML.

## User-visible fixes

- Added an authoritative active-home selector backed by
  `/api/v1/home-scopes`. A single authorized home selects automatically;
  duplicate names show UUIDs; multiple homes require an explicit choice.
- Preserved rc.3's ability to expose an authorized home before the first
  sensor, while moving that authority out of the sensor list and into a
  permission-independent home-scope endpoint. The UI no longer derives its
  active home from sensors or accounts.
- Removed stale cross-home and cross-user UI data during home changes,
  logout, and login. Loading a new home cannot flash the previous home's
  readings or rate plan.
- Bound Dashboard, History/export, Billing, bill imports, SCE refresh, Sensors,
  Circuits, and Home Utility requests to the active home.
- Restored a visible mobile sign-out action and improved long username, sensor,
  alert, draft, release, dialog, and error-message containment.
- Improved dialog accessible names, topmost Escape handling, focus trap and
  restoration, destructive-action disabled state, chart text summaries, tick
  density, and brush targets.

## Backend and API fixes

- Added authenticated `GET /api/v1/home-scopes` independent of enrolled
  devices and billing permissions.
- Added optional `home_id` to home-specific endpoints. Omission resolves only
  for exactly one authorized home; ambiguity returns 422 and cross-home IDs
  return the same 404 as a nonexistent object.
- Bound dashboard timezone, billing cycle, active rate, cost-per-hour, history,
  export, devices, circuits, utility settings, bill drafts, billing overview,
  source status, and manual rate refresh to the exact selected home.
- Kept actor-wide surfaces, such as active alert count, explicitly labeled as
  spanning all authorized homes instead of presenting them as active-home
  data.
- Rejected non-finite electrical values, sample counts above their expected
  count, overlapping loss ranges in one request, and any reading/loss conflict
  detected by the service layer.
- Regenerated the checked-in OpenAPI document for the additive home-scoping
  contract. The firmware protocol identifier remains `pm-protocol/1.0.0`.

## Database fixes

- Added migrations `20260815_0008` through `20260815_0011` instead of
  modifying any applied migration. Migration 0009 introduces the exact-home
  rate-candidate workflow; 0008 and 0010 strengthen ingestion evidence; 0011
  enforces the workflow's database integrity.
- Tightened raw-reading completeness to
  `0 <= sample_count <= expected_sample_count` with positive expected count.
- Added PostgreSQL trigger guards that serialize on the device row and reject
  raw-reading/permanent-loss overlap and loss-range/loss-range overlap,
  including writes that bypass the API.
- Added migration table locks and fail-closed preflight queries. Existing
  conflicts stop the upgrade; no reading or loss evidence is deleted, merged,
  or rewritten.
- Made accepted permanent-loss evidence immutable at both ORM and PostgreSQL
  layers: UPDATE and DELETE are rejected, while new nonconflicting INSERTs
  remain allowed and the earlier raw-reading immutability trigger is preserved.
- Added an always-null database constraint for legacy
  `encrypted_artifact_path`. Existing non-null bill-document references stop
  the upgrade for reviewed remediation rather than being silently deleted.
- Added empty-schema, representative-upgrade, preflight, direct-SQL, and
  concurrency coverage. An isolated PostgreSQL 17 clean upgrade through 0011
  and 0011-to-0010 downgrade/return-to-head passed. The full backend real-
  PostgreSQL suite passed 135 tests with 3 expected skips. The tagged
  public-rc.3 forward-upgrade and target TrueNAS gates remain separate.

## Bill extractor and privacy fixes

- Removed the optional original-document retention configuration and write
  path. New imports never persist an original bill, encrypted or otherwise.
- Preserved the existing deterministic, isolated rate-only pipeline: MIME,
  signature, size, page, encryption, resource, and deadline checks; native
  extraction before bounded OCR; sensitive-region discard; a closed output
  schema; per-field evidence/confidence; review; and separate publication.
- The application retains only allowed rate facts, bounded source coordinates,
  parser provenance, byte/page counts, and the document SHA-256. Original
  bytes and full OCR text cannot enter persistent storage, logs, diagnostics,
  exports, backups, or telemetry.
- Kept prohibited bill categories outside the model: identity, addresses,
  accounts, meter identifiers/readings, usage totals or intervals, balances,
  payments, barcodes/QR values, line totals, and bill totals.
- Added tests proving no source artifact path is written and that the database
  rejects any attempt to set one. Existing rate/usage invariance, sanitized
  digital/OCR/rotation/layout/failure, zero-History, and redaction tests remain.

## Southern California Edison source fixes

- Preserved the official-public-HTTPS-only fetcher with allowlisted hosts,
  public-IP pinning, hostname TLS verification, redirect revalidation, limits,
  deadlines, bounded retry behavior, content checks, and immutable provenance.
- Made source artifact creation crash-durable on Linux by fsyncing the parent
  directory after an atomic replace and when reusing an existing artifact.
- Delayed ETag/Last-Modified advancement until the content is durably stored
  and parsed into a valid review candidate.
- Added safe recovery for the legacy state where a 304 refers to a revision
  without a parsed candidate: perform one unconditional request through the
  same allowlist/SSRF boundary, then parse normally or fail.
- Storage, fetch, parse, layout, and invalid-number failures retain the prior
  validators and last-known-good candidate. They produce failure evidence and
  never publish an empty or guessed rate.
- Added exact-home status, run, candidate, manual-candidate, review, publish,
  activate, reject, and last-known-good operations. Every transition rechecks
  `rates.manage`, scopes the object to the selected home, and keeps review,
  publication, activation, and rejection separate.
- Added a deterministic closed-schema manual fallback for authoritative SCE
  facts. It accepts exact Decimals, confirmed effective dates, an optional
  allowlisted official URL, and a complete validated schedule; canonical JSON
  SHA-256 is its immutable provenance. It accepts no document, free-form bill,
  usage, identity, account, payment, or customer field.
- Added database-backed exact-home manual-candidate idempotency and immutable
  candidate provenance. Review state can advance only through
  `reviewed -> published -> activated` or `reviewed -> rejected`.
- Added a unique natural rate-plan identity and serialized version allocation
  shared by bill and SCE publishing. Assignment replacement is deterministic,
  rejects equal starts, and cannot create overlapping effective ranges.
- Migration 0011 takes PostgreSQL write locks before its preflight and holds
  them through guard installation. The focused workflow/concurrency/direct-SQL
  suite passed all 20 tests.
- Serialized scheduled refresh with a PostgreSQL advisory lease plus
  process-local overlap protection, bounded transient retry/backoff, and one
  source fetch projected into exact-home evidence. Security/parser failures are
  not retried. Per-home alerts and status exclude legacy runs with no home.
- The live official page checked on 2026-08-15 remained incomplete. The parser
  returned `HOLIDAY_RULE_MISSING`; last-known-good data was preserved. This is
  correct fail-closed behavior, not a successful source refresh.

## UI, formatting, and accessibility fixes

- Applied the existing spacing/token system consistently to home selection,
  headers, buttons, forms, cards, alerts, and dialog actions.
- Constrained modal widths/heights at 320 px, made scrollable content and long
  values wrap safely, and prevented page-level horizontal overflow.
- Preserved missing History values as chart gaps, limited tick density by
  available width/range, kept unambiguous tooltip timestamps and units, added
  textual chart summaries, and enlarged brush handles for pointer access.
- Added eight exact responsive viewport checks: 320x568, 375x667, 390x844,
  768x1024, 1024x768, 1280x720, 1440x900, and 1920x1080.
- Added axe checks for all four major pages and the mobile bill-import modal,
  unique-ID checks, geometry-based tick-overlap assertions, overflow checks,
  focus tests, and active-home network-binding tests.
- Local automated evidence is Chromium only; cross-browser and real assistive
  technology remain limitations.

## Sensor, History, and cost fixes

- Preserved HMAC/replay validation and immutable `(device_id, sequence)`
  deduplication while rejecting a sequence that is already covered by signed
  permanent-loss evidence.
- Rejected loss evidence that overlaps a committed reading or a prior loss
  range; identical exact retries remain idempotent.
- Kept untrusted-time readings out of History and retained null-vs-zero
  semantics, PZEM-only completeness, and no interpolation across missing data.
- Bound selected-home History, timezone, rate assignment, and cost calculation
  together so authorized homes cannot be mixed. Decimal/NUMERIC cost lineage
  and immutable rate-version selection remain unchanged.

## Firmware fixes and compatibility

- No firmware runtime or wire-protocol change was required. Public rc.3 already
  returned authorized home scopes with `/devices`, even before first
  enrollment, and the server enrolls from a one-time token owned by a home.
  Rc.4 decouples browser-wide home discovery into `/home-scopes` and removes
  ambiguity from every home-specific operation.
- Reviewed the independent firmware source, host/fault/HIL definitions,
  workflows, release metadata, and server contract. Added `.gitattributes` so
  text and shell sources remain LF in Linux CI, and corrected the documented
  Python host-test count from 55 to 59.
- Retained `pm-protocol/1.0.0`, outbound HTTPS-only behavior, microSD journal,
  ordered replay/dedupe, bounded reconnect/backoff, and pending physical
  certification. Historical simulation/build results are not relabeled as
  current hardware evidence.

## Storage, backup, and deployment fixes

- Replaced the normal shell/SSH preparation dependency with a supported
  Windows + TrueNAS UI flow for the next release: create nine POSIX ZFS
  datasets, temporarily share only `secrets`, stage exactly 13 files over
  authenticated SMB, disable the share, paste the complete signed YAML, and
  install.
- Added `Stage-PowerMeterTrueNAS.ps1`. It accepts only a trusted local source,
  validates the exact file set and independent decoded secrets, validates the
  TLS key/hostname/strict chain/current time/seven-day horizon, copies through a
  same-share staging directory, byte-verifies, and writes its marker last. On
  failure it removes only files moved by that invocation.
- Added a one-shot `initialize` service using the digest-pinned API image. It
  has no network, a read-only root, all capabilities dropped except the exact
  file-ownership capabilities, and fixed host mounts. It never creates ZFS
  datasets or missing top-level bind roots, generates secrets, rotates values,
  or remains running.
- The initializer installs two image-owned configuration assets atomically,
  creates required child directories, repairs/verifies exact UID/GID/mode/ACL
  state, and blocks PostgreSQL/migration/runtime startup on any mismatch.
- Reduced new-install storage to nine datasets; the prohibited bill-original
  dataset is not mounted or created. An old rc.3 dataset is left unmounted and
  is never auto-deleted.
- Kept encrypted backup and isolated restore identities separate. The API has
  traversal/read access only to bounded backup status, not archive contents.
- Corrected release smoke to exercise a genuine first run, use certificates
  valid beyond the seven-day horizon, and restart only long-running services.

## Security fixes

- Prevented cross-home object discovery, stale protected browser caches, and
  persistence of original bills.
- Preserved strict TLS chain/hostname checks and added whole-chain future-time
  verification to both SMB staging and initialization.
- Isolated long-running services from the secrets directory; each receives
  only its declared secret files. The temporary SMB ACL is replaced by exact
  numeric readers and the share must be disabled before installation.
- Added a local-Docker endpoint gate and complete `PM_*` environment isolation
  to the audit runner; database migrations require an explicit disposable
  integration option.
- Updated the secret-scan action to the reviewed immutable gitleaks v3 commit.
  No verification bypass, broad privilege, Docker socket, host networking,
  plaintext production secret, or floating production image tag was added.

## Dependency and workflow changes

- Updated the explicitly pinned development `PyYAML` patch from 6.0.2 to 6.0.3.
- Updated `gitleaks/gitleaks-action` from the reviewed v2 pin to immutable v3
  commit `e0c47f4f8be36e29cdc102c57e68cb5cbf0e8d1e` for the current Node runtime.
- Added workflow gates for Ruff formatting, Linux-platform mypy of the
  initializer, PowerShell parsing of the stager, and exact release/stable asset
  comparison.
- No uncontrolled bulk dependency upgrade was performed. Current CI,
  dependency audit, image scan, attestation, and release execution remain
  pending.

## Principal test additions

- `backend/tests/test_home_selection.py`
- `backend/tests/test_bill_artifact_storage.py`
- `backend/tests/test_ingestion_database_guards.py`
- `backend/tests/test_rate_candidate_workflow.py`
- expanded ingestion, authorization, rate-sync, migration, release, and
  deployment tests
- `frontend/tests/e2e/home-scope.spec.ts`
- `frontend/tests/e2e/responsive-accessibility.spec.ts`
- `frontend/tests/ui.test.tsx`
- expanded auth, home, History, billing, settings, fixtures, mocks, and browser
  tests
- `tests/test_host_initializer.py`
- `scripts/Invoke-PowerMeterFullAudit.ps1` and its safety-oriented validation

Passing historical rc.3 evidence and current local evidence are deliberately
kept separate. See `VALIDATION_MATRIX.md` for the current result of each gate.
The full backend real-PostgreSQL suite passed 135 tests with 3 expected skips;
Ruff lint/format and mypy across 81 files passed. Frontend lint/type/build, 24
Vitest tests, and 36 Chromium Playwright tests passed. No whole-repository
aggregate is claimed, and these local results do not replace the tagged
public-rc.3 forward-upgrade, four-image Docker, remote CI, target TrueNAS,
publication, or HIL gates.
