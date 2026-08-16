# PowerMeter V2 v0.1.0-rc.8

PowerMeter V2 is a central, authenticated PZEM-004T monitoring system paired
with the independent
[`power-monitor-sensor-headless`](https://github.com/mhilton7/power-monitor-sensor-headless)
firmware. The shared device contract remains `pm-protocol/1.0.0`.
Authenticated sensor evidence remains the only source of live measurements,
History, energy, completeness, forecasts, and usage-based cost. Utility-bill
PDFs remain rate-source documents only.

This source file is the release-body input for candidate `v0.1.0-rc.8`. This
source copy alone is not publication evidence. Installation is authorized only after the
signed tagged workflow publishes the complete checksummed and attested asset
set, digest-pinned YAML, public multi-architecture images, deployment evidence,
and coordinated firmware release.

## Why rc.8 follows public server rc.6

Server [`v0.1.0-rc.6`](https://github.com/mhilton7/power-monitor-v2/releases/tag/v0.1.0-rc.6)
completed its tagged workflow and remains a public, immutable, installable
prerelease with its complete attached asset set. Rc.8 carries its SCE,
rate-only bill parsing, user/profile, home naming, sensor presentation, and
per-user settings behavior forward unchanged.

Rc.8 bounds authenticated heartbeat command responses to the ESP32 receive
buffer, prevents the firmware's uint8 delivery attempt from overflowing, and
terminalizes linked OTA deployments when delivery can no longer complete. The
browser accepts exact decimal telemetry strings as finite nonnegative numbers
while preserving missing values as null and measured zero as zero. Bill-rate
extractions now accept their required `home_id` only when it matches the
selected home. It retains `pm-protocol/1.0.0` and Alembic head
`20260815_0012`; no migration or breaking sensor-contract change is introduced.
Its generated OpenAPI SHA-256 is
`b7f8726f73633bd577da2cd3a9bfb7a2104615dafb44681564cd81fce8c8148f`.

### Historical rc.4 recovery carried by rc.5

Server `v0.1.0-rc.4` remains a valid, immutable signed tag. Tagged release run
[`31893354667`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31893354667)
passed the workflow's named `Mandatory release gates` job, published all four
multi-architecture images, and
proved anonymous GHCR access. Its digest-pinned deployment smoke then failed
deterministically: `docker compose start` traversed service dependencies and
restarted the already-completed one-shot initializer, so its preserved
completion-time assertion correctly failed even though the runtime services
returned healthy. Release assembly was skipped. There is no server rc.4 GitHub
Release or generated rc.4 YAML, and the published rc.4 images alone are not an
installation authority.

Rc.5 preserved the audited application, protocol, and migration behavior while
repairing that recovery check. It captures the six runtime container IDs,
starts those exact stopped containers directly without Compose dependency
traversal, requires their identities and health to remain stable, and still
proves that the initializer ID, exit state, and completion time are unchanged.
Failure evidence now records only a fixed allowlisted assertion identifier in
addition to the existing redacted diagnostics. No workflow, API behavior,
protocol, or migration revision changed for this repair.

## What changed since public v0.1.0-rc.3

### Home isolation and browser behavior

- The authenticated API exposes only the actor's authorized home scopes.
- Home, History, export, Billing, device, circuit, utility-setting, rate-check,
  and bill-import queries bind an exact selected home UUID. An omitted home is
  accepted only when exactly one authorized scope exists; ambiguous, absent,
  or cross-home selections fail closed.
- Dashboard timezone, account, rate, and per-device cost calculations now use
  the exact home associated with the selected evidence instead of an unrelated
  first row.
- The browser auto-selects only a sole authorized home, requires an explicit
  choice for multiple homes, uses ordinal labels when names collide without
  exposing UUIDs, clears stale queries when the actor or home changes, and
  never grants authority from client state.
- Mobile navigation, dialogs, focus management, loading/error/empty states,
  chart descriptions, gaps, brush targets, responsive containment, and
  keyboard behavior were repaired and covered at the supported viewports.

### Bill-rate privacy and source reliability

- Original utility-bill bytes are released after bounded parsing and are never
  persisted, logged, or returned, encrypted or otherwise. Only allowlisted
  rate facts, provenance, validation status, and redacted diagnostics may be
  retained. A database constraint prevents new original-artifact references.
- Upgrade preflight rejects any historical retained-document reference for
  operator review. It never deletes a document or rewrites evidence silently.
- SCE refresh keeps the last known good candidate when parsing, storage, or the
  official source fails. HTTP validators advance only after a usable candidate
  exists; legacy `304` state without a parsed candidate receives one bounded
  unconditional recovery request through the same SSRF and size controls.
- Immutable source artifacts are file- and directory-synced before database
  pointers advance. Holiday evidence is required only for time-dependent
  schedules; DOMESTIC flat/tiered plans use `not_applicable`, while genuinely
  unresolved TOU treatment remains an actionable failure.
- The configured public SCE source is the tiered-rate page. Classification
  prefers its primary Tier 1/Tier 2 tariff content over incidental shared TOU
  navigation/FAQ copy. A valid unchanged fetch is reported as success with no
  duplicate candidate or version.
- The closed bill parser accepts a complete statement or an isolated numbered
  SCE charges page. It extracts exact Decimal rate components, billing-period
  context, and provenance; approximate chart prices and already-included
  explanatory breakdowns are ignored. The supplied DOMESTIC page remains
  review-required because one customer-period baseline and one season cannot
  establish a complete reusable annual tariff.
- The exact-home candidate workflow now supports deterministic, database-
  idempotent manual candidates plus explicit review, publication, activation,
  and rejection. Candidate provenance is immutable. The only legal review
  paths are `reviewed -> published -> activated` or `reviewed -> rejected`;
  fetching, parsing, reviewing, publishing, or rejecting never auto-activates a
  rate.

### Accounts, naming, sensors, and settings

- Administrators can create, edit, enable/disable, reset passwords for,
  soft-delete, and restore scoped users. Email uniqueness is case-insensitive,
  the last active home owner is protected, security-sensitive changes revoke
  sessions, and audit details never contain password material.
- Regular users can update their own display name, reauthenticated email,
  password, and persisted display preferences without gaining administrative
  permissions.
- Existing UUID-shaped `Home (<uuid>)` labels are normalized to `Home` without
  changing the internal UUID or any device/history relation. Home rename
  updates the selector cache immediately; the UUID is shown only in Advanced
  system health with an explicit copy action.
- Sensor location, notes, order, aggregate eligibility, dashboard visibility,
  and monitoring state persist at device scope. Per-user refresh, range,
  units, date/time, precision, density, and card-visibility preferences are
  applied by Home and History rather than stored as disconnected controls.

### Ingestion and database integrity

- Alembic head `20260815_0012` extends the frozen chain without rewriting an
  applied migration. Revision 0008 enforces
  `sample_count <= expected_sample_count` and prevents authenticated raw
  readings from overlapping permanent-loss evidence or two loss ranges from
  overlapping for one device. Revision 0009 adds the exact-home rate-candidate
  review lifecycle. Revision 0010 makes authenticated permanent-loss rows
  immutable against UPDATE and DELETE and repairs raw trigger ordering without
  weakening the pre-existing raw-reading immutability guard. Revision 0011
  enforces exact-home manual-candidate idempotency, immutable candidate
  provenance and legal review transitions, a unique natural rate-plan identity
  with serialized version allocation shared by bill and SCE publication, and
  deterministic non-overlapping assignments with equal-start rejection.
  Revision 0012 fails closed on case-insensitive duplicate emails, normalizes
  safe legacy home labels, and adds per-user preferences, sensor presentation
  settings, and typed bill-rate evidence fields without changing home, device,
  reading, rate-version, or audit identities.
- The migrations take write-blocking table locks, preflight existing evidence,
  and fail closed before installing PostgreSQL guards. Revision 0011 also locks
  its rate workflow tables across preflight and guard installation. Conflicting
  immutable evidence is preserved for review; it is never merged or deleted.
- Application writers continue to serialize per device, and direct database
  writes receive stable SQLSTATE and constraint failures.

### TrueNAS no-shell installation

- The generated YAML contains eight services. A one-shot, network-isolated
  `initialize` service reuses the exact API image digest, validates the staged
  13 secret/TLS files, installs image-embedded configuration, and applies and
  verifies the required owners, modes, and ACLs before PostgreSQL or any
  long-running service starts.
- Normal installation uses only the TrueNAS web UI, a temporary authenticated
  SMB share, and the tracked Windows staging helper. SSH, the TrueNAS shell,
  a container console, privileged mode, host networking, and the Docker socket
  are not part of the normal path.
- Operators create exactly nine Generic/POSIX child ZFS datasets below
  `/mnt/Apps/PowerMeterV2`. The former `bill-rate-source-artifacts` dataset is
  not mounted by rc.5, rc.6, or rc.8; an existing rc.3 dataset is left untouched and should
  remain unshared until an operator chooses a separately reviewed cleanup.
- TLS verification remains strict for `power-monitor.home.arpa`, including
  hostname, key match, current validity, and at least seven days of remaining
  validity across the complete chain. This release requires the DNS hostname;
  direct-IP HTTPS is not supported and verification must never be bypassed.

### Audit and release hardening

- The Windows audit orchestrator resolves the repository root, records start
  and end state, runs format/lint/type/test/build/security checks, refuses a
  remote Docker endpoint, strips inherited `PM_*` values, and makes disposable
  database/Compose work explicit. Its default mode never migrates a database.
- Release evidence now requires the exact permission record set and rejects
  duplicate, blank, contradictory, or extra records.
- Gitleaks is pinned to the official Node 24 action revision. Production images
  and Actions remain immutable-pinned; no production image uses `latest`.

## Upgrade and rollback boundary

Back up and verify the database, perform an isolated restore check, and take a
recursive ZFS snapshot before installing rc.8. Keep all existing application
secrets, database credentials, the backup encryption key, TLS material, and
datasets. Do not recreate storage or rotate secrets during this upgrade.

The migration chain through `20260815_0012` is intentionally fail-closed. The
0008 preflight blocks an overlap in immutable reading/loss evidence or a
historical retained-bill reference for review. Revision 0010 requires the exact
legacy raw-reading immutability guard before changing trigger ordering.
Revision 0011 stops on duplicate natural rate plans, invalid or overlapping
assignments, or inconsistent candidate-review evidence and holds PostgreSQL
write locks until its guards are installed. Do not bypass a guard, edit an
applied migration, or delete evidence merely to continue.

The release migration report proves only the forward upgrade from the latest
lower same-major public release. It does not prove that rc.6 binaries can use
state touched by rc.8. Application-only rollback is not authorized. A
rollback requires a separately validated restore or clone of the matching
pre-upgrade snapshot or verified backup, paired with the exact older release
assets. GitHub-hosted smoke records rollback as
`not_exercised_github_hosted_smoke`.

## Release contents and firmware pairing

A successfully published rc.8 candidate includes:

- four immutable multi-architecture GHCR images referenced by registry digest;
- `power-monitor-v2-v0.1.0-rc.8.yaml` and `release-manifest.json`;
- the Windows SMB staging helper and auditable initializer source;
- checksums, strict attestations, SBOMs, vulnerability results, dependency and
  test reports, migration evidence, and deployment-smoke evidence;
- installation, secrets/TLS, dataset/ACL, backup/restore, upgrade, and rollback
  instructions; and
- an exact coordinated public firmware `v0.1.0-rc.8` release whose server
  contract declares OpenAPI SHA-256
  `b7f8726f73633bd577da2cd3a9bfb7a2104615dafb44681564cd81fce8c8148f`.

The signed server rc.2 tag and failed release run `31866197054` remain
immutable prepublication evidence. There is no server rc.2 GitHub Release.
Public server rc.6 remains installable with its own attached eight-service
assets and instructions and is the migration predecessor for rc.8. Firmware
rc.8 must be published and verified with the exact server rc.8 contract before
a server rc.8 tag is created. No historical tag or release is modified or
relabeled by rc.8; rc.7 remains an unpublished local firmware candidate.

This remains a prerelease candidate. Hardware status is honestly `pending`.
Marked-unit electrical identity, physical TLS/HMAC behavior, OTA installation
and rollback, outage/recovery cycling, and a continuous measured soak of at
least 72 hours must produce schema-valid machine evidence before stable
promotion can open. Simulation, host tests, CI, or candidate publication cannot
substitute for those physical results.

Review `release-manifest.json`, `hardware-certification-status.json`,
`SHA256SUMS`, and the attached `INSTALLATION.md`. Never install the repository
template containing `UNPUBLISHED_*`, substitute a floating image tag or local
Docker ID, bypass TLS or PDF-sandbox readiness, or use a utility bill as usage
evidence.
