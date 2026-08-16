# Known limitations

This list preserves the rc.5-preparation snapshot from the 2026-08-15 audit.
Server rc.6 subsequently completed publication and remains public and
installable with its attached assets. Hardware execution confirmed that
firmware rc.1 through rc.5 crash in the main stack before provisioning;
coordinated rc.8 publication is pending. Historical evidence is never
relabeled as proof for a later candidate.

## 1. The repaired checkout is not an installable release

- **Exact limitation:** `deploy/truenas/power-monitor-v2.yaml` intentionally
  contains `UNPUBLISHED_*` image-digest sentinels. Public `v0.1.0-rc.3` is
  immutable and does not contain the no-shell initializer or Windows stager.
- **Reason:** A release YAML is valid only after four images are built, scanned,
  published, attested, and substituted by the coordinated release workflow.
  Server rc.4 completed the image and anonymous-access gates, but deterministic
  deployment smoke failed and assembly was skipped, so those images have no
  Release/YAML authority. The rc.5 OpenAPI hash requires a distinct
  firmware-first coordinated rc.5 freeze; rc.3 or rc.4 metadata cannot be
  rewritten.
- **User impact:** Pasting the source-tree YAML cannot install this repair.
- **Workaround:** Continue using rc.3 only with its attached rc.3 assets and
  instructions; do not mix rc.3 with current source files.
- **Recommended next repair:** Finish all local gates, coordinate the unchanged
  `pm-protocol/1.0.0` metadata with firmware, and publish a new signed candidate
  (planned rc.5) without moving or replacing rc.3 or rc.4.

## 2. Rc.5 tagged predecessor and target PostgreSQL gates are pending

- **Exact limitation:** Local PostgreSQL 17 evidence now proves the migration
  chain through `20260815_0011`, an 0011-to-0010-to-head exercise, all 20
  rate-workflow concurrency/direct-SQL cases, and the full backend suite at 135
  passed with 3 expected skips. Server rc.4's exact tagged public-rc.3
  forward-upgrade gate also passed, but that is rc.4-only evidence. Neither is
  an rc.5 tagged gate or target-TrueNAS execution.
- **Reason:** A local disposable PostgreSQL run cannot create signed tagged
  workflow evidence or prove the target host's storage, roles, and cutover.
- **User impact:** Database guards are locally executed, but an rc.5 upgrade
  must not be represented as released or target-proven.
- **Workaround:** Do not deploy this checkout. Preserve a verified encrypted
  backup and ZFS snapshot before any eventual candidate upgrade.
- **Recommended next repair:** Run the exact tagged public-rc.3 forward-upgrade
  gate, then repeat the release-specific migration/backup/restore procedure on
  the target TrueNAS host and retain redacted evidence.

## 3. Existing evidence conflicts deliberately block migration

- **Exact limitation:** Migration `20260815_0008` stops if a raw reading
  overlaps authenticated permanent-loss evidence or two loss ranges overlap
  for a device.
- **Reason:** Automatically deleting, merging, or rewriting either immutable
  source would destroy provenance.
- **User impact:** An affected installation remains on its prior version until
  the conflicting evidence is reviewed.
- **Workaround:** Leave the prior datasets intact, preserve the exact failure,
  backup, and snapshot; continue the prior signed release while a reviewed
  recovery decision is made.
- **Recommended next repair:** Develop a separately approved, auditable
  evidence-adjudication procedure. Do not weaken or bypass the migration.

## 4. Legacy retained bill references deliberately block migration

- **Exact limitation:** A non-null legacy
  `utility_bill_rate_uploads.encrypted_artifact_path` stops migration. The new
  runtime never persists original bills, encrypted or otherwise, but does not
  auto-delete a legacy operator file.
- **Reason:** Silent deletion could violate retention obligations or destroy
  evidence; continuing would violate the new no-original-persistence invariant.
- **User impact:** An installation that enabled old optional retention cannot
  upgrade automatically.
- **Workaround:** Keep the legacy `bill-rate-source-artifacts` dataset unmounted
  and unshared. Do not export, decrypt, or expose it. Continue the prior release
  until an authorized privacy procedure exists.
- **Recommended next repair:** Provide a separately reviewed operator workflow
  that verifies backup/authority, removes prohibited originals and database
  references safely, and records only non-sensitive completion evidence.

## 5. Rc.5 release images and release-specific deployment smoke are pending

- **Exact limitation:** The retained strict local audit passed cache-only builds
  of backend, frontend, gateway, and backup, both Compose validations, and a
  disposable runtime with API, database, PDF sandbox, worker, and frontend
  healthy followed by exact resource cleanup. Rc.4 later published four signed
  multi-architecture images and ran the full production-digest smoke, encrypted
  backup, and isolated restore, but its deterministic runtime-recovery check
  restarted the initializer dependency and failed before assembly. Rc.5 has no
  published images or release-specific deployment smoke yet.
- **Reason:** Cache-only local builds and the disposable application stack do
  not produce signed digests, SBOMs, provenance, attestations, or
  release-specific recovery evidence.
- **User impact:** Local image construction and service interaction have
  retained evidence, but rc.4 is not installable and rc.5 has no release
  recovery proof.
- **Workaround:** Use no current-source image or local Docker ID in production;
  use only immutable published release digests.
- **Recommended next repair:** Publish only through the coordinated signed
  workflow, run full release smoke against those exact digests and the rendered
  YAML, and preserve redacted backup/restore evidence.

## 6. The no-shell model has not run on the target TrueNAS host

- **Exact limitation:** The nine-dataset, temporary-SMB, eight-service model has
  not completed clean install, upgrade, ACL, restart, backup/restore, and
  recovery checks on `192.168.0.175`.
- **Reason:** A container namespace cannot conclusively prove that each bind is
  a distinct ZFS dataset; that precondition and TrueNAS Custom App behavior need
  target execution.
- **User impact:** The procedure is implemented and statically tested but not
  yet operationally certified on the user's host.
- **Workaround:** Do not use the unpublished source template. Keep rc.3 and all
  datasets intact until the next candidate is published and verified.
- **Recommended next repair:** Run the release-specific target suite through
  the TrueNAS UI, record exact tags/digests/dataset roots/ACLs/backup and restore
  IDs, and retain redacted machine-readable evidence.

## 7. Rollback compatibility remains unproved

- **Exact limitation:** Forward migration evidence never proves that old
  binaries can read a database touched by a newer migration. No current
  release-specific restored rc.5-to-rc.3 rollback exists; rc.3 has no
  initializer and uses its own attached seven-service preparation contract.
- **Reason:** Attaching old binaries to the upgraded production database risks
  corruption or data loss.
- **User impact:** Application-only rollback is not authorized.
- **Workaround:** Preserve the old YAML/digests plus a matching pre-upgrade ZFS
  snapshot and verified encrypted backup. Never roll the sole live database
  backward in place.
- **Recommended next repair:** Restore a matching pre-upgrade backup into an
  isolated target, validate the prior release there, and document a tested
  cutover before permitting rollback.

## 8. The live SCE page provides rounded display rates

- **Exact limitation:** The official public Tiered Rate Plan page read on
  2026-08-15 now parses as `DOMESTIC` seasonal tiered with holiday treatment
  `not_applicable`, but its public-facing energy prices are rounded display
  values rather than the five-decimal component rates printed on a bill.
- **Reason:** Shared page navigation contains TOU material, while the primary
  DOMESTIC Tier 1/Tier 2 content is a separate semantic region. The parser now
  gives that primary evidence precedence, but it does not invent precision the
  page does not publish.
- **User impact:** `HOLIDAY_RULE_MISSING` is no longer emitted for this valid
  tiered source. Any changed candidate still requires explicit review before
  publish and activation.
- **Workaround:** Review the exact-home candidate and use authoritative tariff
  or privacy-safe bill rate evidence when five-decimal precision is required.
  Last-known-good evidence remains active until that workflow completes.
- **Recommended next repair:** Retain a sanitized fixture and live semantic
  probe so navigation/template changes cannot silently reclassify the plan.

## 9. Browser and accessibility coverage is not cross-engine or physical

- **Exact limitation:** The 36 browser tests, eight responsive sizes, axe
  checks, and manual in-app checks use Chromium with deterministic mocked API
  data. Firefox, WebKit, real screen readers, switch/voice input, browser zoom,
  OS high-contrast, and physical touch devices were not exercised.
- **Reason:** Those environments were outside the local run.
- **User impact:** Engine-specific layout or assistive-technology defects may
  remain despite the passing Chromium/axe evidence.
- **Workaround:** Use the tested Chromium path and report any engine/AT-specific
  issue without suppressing accessibility checks.
- **Recommended next repair:** Add Firefox/WebKit Playwright projects and a
  manual NVDA/VoiceOver, keyboard, zoom, contrast, and touch-device acceptance
  matrix.

## 10. Baseline browser evidence was partly fixture-based

- **Exact limitation:** The baseline responsive sweep used the deterministic
  fixture API and did not retain a HAR or a screenshot for every route/viewport
  combination. The current UI browser checks also do not prove backend,
  database, sensor, or network behavior.
- **Reason:** Fixture isolation made layout/state failures reproducible but
  intentionally removed external services.
- **User impact:** A passing UI test cannot establish a healthy live sensor or
  PostgreSQL path.
- **Workaround:** Treat browser evidence as frontend evidence only and use API,
  PostgreSQL, Compose, and target deployment evidence for the other layers.
- **Recommended next repair:** Add a current-candidate end-to-end run against
  disposable PostgreSQL/containers, preserving bounded redacted network and
  runtime artifacts.

## 11. Rc.5 remote CI, security, and release workflows have not run

- **Exact limitation:** Workflow edits, gitleaks v3, dependency audits, CodeQL,
  container scans, SBOMs, provenance, attestations, and release assembly have
  no GitHub run for this uncommitted rc.5 working tree. Rc.4's CI/security and
  release gates are immutable historical evidence: all mandatory, image, and
  anonymous-access jobs passed, deployment smoke failed, and assembly skipped.
- **Reason:** Local source and workflow validation cannot create remote build
  or publication evidence.
- **User impact:** No security or release claim can be made for a next
  candidate, and there are no valid next-candidate GHCR digests.
- **Workaround:** Do not substitute local image IDs, floating tags, or rc.3/rc.4
  attestations for rc.5 evidence.
- **Recommended next repair:** Commit reviewed changes, run CI/security on the
  exact commit, then use the coordinated signed release workflow only after all
  mandatory gates pass.

## 12. Physical hardware certification is pending

- **Exact limitation:** No machine-readable evidence from the actual marked
  ESP32-S3/PZEM-004T/microSD unit proves identity, wiring/electrical behavior,
  PZEM readings, TLS/HMAC, offline replay, SD-full/corruption/power loss,
  Wi-Fi/DNS/server outage recovery, watchdog/heap/stack, OTA rollback, USB
  recovery, physical cycles, or a continuous 72-hour soak.
- **Reason:** Host tests, fault injection, simulation, and reproducible builds
  are not physical hardware evidence.
- **User impact:** Firmware and server releases remain prerelease candidates;
  stable promotion is blocked.
- **Workaround:** Use only as a prerelease monitoring system under qualified
  electrical installation and retain sensor microSD data during outages.
- **Recommended next repair:** Run the signed candidate's HIL plan on the
  marked unit and attach complete, checksummed, machine-readable evidence tied
  to exact firmware binary hash and commit.
