# TrueNAS upgrade

Never edit only the image tag. Upgrade by replacing the entire old generated YAML with the new signed, digest-pinned release asset.

1. Read both server and compatible firmware release notes. Confirm `pm-protocol/1.0.0` compatibility and migration direction.
2. Verify the new release manifest, Compose checksum, SBOMs, and provenance. Record the current app version and all current digests from the old manifest.
3. Trigger a fresh encrypted backup and an isolated restore test. Stop if either evidence status is not `verified`.
4. Take ZFS snapshots of the V2 datasets as an additional recovery point. Do not snapshot or copy unencrypted secret material into a less protected dataset.
5. Paste the new complete YAML into TrueNAS **Edit > YAML** and save. The one-shot `migrate` service runs before application services.
6. Confirm every health check, then test owner login, SSE live updates, committed History, a duplicate ingestion retry, a rate calculation fixture, command queue delivery, firmware inventory, backup status, and diagnostics redaction.
7. Run a new backup and restore test under the new version. Retain the old release manifest and image digests for the rollback window.

Database migrations must be forward-safe and tested from both a clean database and the immediately previous V2 version. There is no supported in-place upgrade from the legacy `power-monitor` database; see `docs/MIGRATION.md`.
