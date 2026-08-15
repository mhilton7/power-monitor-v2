# PowerMeter V2 v0.1.0-rc.5

PowerMeter V2 is a central, authenticated PZEM-004T monitoring system paired
with the independent
[`power-monitor-sensor-headless`](https://github.com/mhilton7/power-monitor-sensor-headless)
firmware. The shared device contract remains `pm-protocol/1.0.0`.
Authenticated sensor evidence remains the only source of live measurements,
History, energy, completeness, forecasts, and usage-based cost. Utility-bill
PDFs remain rate-source documents only.

This source file is the release-body input for candidate `v0.1.0-rc.5`. This
source copy alone is not publication evidence. Installation is authorized only after the
signed tagged workflow publishes the complete checksummed and attested asset
set, digest-pinned YAML, public multi-architecture images, deployment evidence,
and coordinated firmware release.

## Why rc.5 follows the non-installable rc.4 tag

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

Rc.5 preserves the audited application, protocol, and migration behavior while
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
  UUID-disambiguated choice for multiple homes, clears stale queries when the
  actor or home changes, and never grants authority from client state.
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
  pointers advance. Missing official holiday or effective-date evidence is an
  actionable failure, never fabricated rate data.
- The exact-home candidate workflow now supports deterministic, database-
  idempotent manual candidates plus explicit review, publication, activation,
  and rejection. Candidate provenance is immutable. The only legal review
  paths are `reviewed -> published -> activated` or `reviewed -> rejected`;
  fetching, parsing, reviewing, publishing, or rejecting never auto-activates a
  rate.

### Ingestion and database integrity

- Alembic head `20260815_0011` extends the frozen chain without rewriting an
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
  not mounted by rc.5; an existing rc.3 dataset is left untouched and should
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
recursive ZFS snapshot before installing rc.5. Keep all existing application
secrets, database credentials, the backup encryption key, TLS material, and
datasets. Do not recreate storage or rotate secrets during this upgrade.

The migration chain through `20260815_0011` is intentionally fail-closed. The
0008 preflight blocks an overlap in immutable reading/loss evidence or a
historical retained-bill reference for review. Revision 0010 requires the exact
legacy raw-reading immutability guard before changing trigger ordering.
Revision 0011 stops on duplicate natural rate plans, invalid or overlapping
assignments, or inconsistent candidate-review evidence and holds PostgreSQL
write locks until its guards are installed. Do not bypass a guard, edit an
applied migration, or delete evidence merely to continue.

The release migration report proves only the forward upgrade from the latest
lower same-major public release. It does not prove that rc.3 binaries can use a
database touched by rc.5. Application-only rollback is not authorized. A
rollback requires a separately validated restore or clone of the matching
pre-upgrade snapshot or verified backup, paired with the exact older release
assets. GitHub-hosted smoke records rollback as
`not_exercised_github_hosted_smoke`.

## Release contents and firmware pairing

A successfully published rc.5 candidate includes:

- four immutable multi-architecture GHCR images referenced by registry digest;
- `power-monitor-v2-v0.1.0-rc.5.yaml` and `release-manifest.json`;
- the Windows SMB staging helper and auditable initializer source;
- checksums, strict attestations, SBOMs, vulnerability results, dependency and
  test reports, migration evidence, and deployment-smoke evidence;
- installation, secrets/TLS, dataset/ACL, backup/restore, upgrade, and rollback
  instructions; and
- an exact coordinated public firmware `v0.1.0-rc.5` release whose server
  contract declares OpenAPI SHA-256
  `66b4e1cfb0f5a5797dadd9a8783ff0b192ca416d1f4264c135a4e380b2b94591`.

The signed server rc.2 tag and failed release run `31866197054` remain
immutable prepublication evidence. There is no server rc.2 GitHub Release.
Public server rc.3 remains the installation authority and migration predecessor
for its own attached seven-service assets and instructions. Public firmware
rc.4 is immutable historical evidence for the failed server rc.4 attempt; the
coordinated firmware rc.5 target must be published and verified before a server
rc.5 tag is created. No historical tag or release is modified or relabeled by
rc.5.

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
