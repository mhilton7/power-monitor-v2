# PowerMeter V2 v0.1.0-rc.1

PowerMeter V2 is a greenfield central server coordinated with the independent
[`power-monitor-sensor-headless`](https://github.com/mhilton7/power-monitor-sensor-headless)
firmware repository through `pm-protocol/1.0.0`. Authenticated PZEM-004T sensor
evidence is the only source of live measurements, History, energy, forecasts,
completeness, and usage-based cost. Utility-bill PDFs are rate-source documents
only.

This file is the release-body input in source control; its presence alone does
not prove a release workflow ran. The public prerelease and its attached
evidence are authoritative. The tagged workflow publishes `v0.1.0-rc.1` only
after all required nonphysical gates pass, the coordinated public firmware
prerelease and contracts verify, every GHCR digest resolves anonymously, and
the complete release artifact set is checksum-valid and attested.

A successfully published candidate includes:

- immutable multi-architecture API, frontend, gateway, and backup images in GHCR, each
  referenced by registry-reported SHA-256 digest;
- a complete generated `power-monitor-v2-v0.1.0-rc.1.yaml` suitable for TrueNAS
  **Apps > Install via YAML**;
- `release-manifest.json`, per-image records, SPDX SBOMs, security results,
  test/migration/deployment reports, checksums, and GitHub attestations;
- `Caddyfile`, `postgres-init-roles.sh`, the checked `prepare-host.sh`, and
  complete install, dataset/ACL, secret/TLS, first-run, backup/restore, upgrade,
  and rollback guides;
- compatible firmware identity and explicit hardware-certification status.

The release workflow's nonphysical deployment smoke covers the exact seven
services, immutable image startup, database migration and role separation, TLS
chain/hostname, API readiness and PDF sandbox, authenticated enrollment/PZEM
ingestion, PZEM-only History and cost, commands, SSE, upload limits, dataset
permissions, per-service and full-stack restart, encrypted backup, and isolated
restore. For this initial V2 release there is no previous V2 schema to upgrade
or roll back from; legacy `power-monitor` code/database import is unsupported.

This is a release candidate, not a stable or physically certified product.
`hardware-certification-status.json` remains `pending`. Marked-unit electrical
interface verification, certificate/hostname behavior on hardware, OTA success
and rollback, physical fault/recovery, and a measured soak of at least 72 hours
must produce schema-valid machine evidence before stable promotion can open.
Simulation, host tests, a successful ESP-IDF build, or publication of this
prerelease cannot substitute for those physical results.

Review `release-manifest.json`, `RELEASE_NOTES.md`,
`hardware-certification-status.json`, and `SHA256SUMS`, then follow the attached
`INSTALLATION.md`. Never install the repository template containing
`UNPUBLISHED_*`, substitute a floating image tag/local Docker ID, bypass TLS or
PDF-sandbox readiness, or treat a utility bill as usage evidence.

Release scope and gates are defined in `docs/RELEASE_PROCESS.md`; migration
boundaries are in `docs/MIGRATION.md`.
