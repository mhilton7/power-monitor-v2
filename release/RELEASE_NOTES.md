# PowerMeter V2 release-candidate notes

PowerMeter V2 is a greenfield central server coordinated with the independent `power-monitor-sensor-headless` firmware repository through `pm-protocol/1.0.0`.

This source state is not a published release. GHCR digests, attestations, release URLs, migration evidence from a real previous V2 release, clean/upgrade/rollback deployment results, and hardware-in-loop certification do not exist until the corresponding workflows complete. The production Compose template therefore contains intentional `UNPUBLISHED_*` sentinels and cannot be deployed accidentally.

The target server and firmware repositories do not yet exist on GitHub. Their
visibility must be verified as public, matching the verified public reference
repository, before publication is allowed. Current `gh` authentication is
invalid, and no signing key/tool is registered or configured, so repository
creation, signed tags, GHCR publication, attestations, and releases are blocked.
No unsigned substitute is permitted. The server release workflow also requires
anonymous access to each exact GHCR image digest.

The 2026-08-14 local candidate passed the feasible server, PostgreSQL-role,
frontend/browser, PDF-sandbox, contract, backup/restore, firmware host/fault/C,
sanitizer, reproducible-build, and 120-day simulation gates recorded in
`docs/TESTING.md`. Its three application Docker IDs are local-only and are not
publishable registry identities. The earliest permissible publication remains
prerelease `v0.1.0-rc.1` after authentication/signing and every nonphysical
release gate, including the public digest-pinned seven-service smoke, passes.
Stable status is independently blocked pending marked-hardware TLS, OTA
success/rollback, physical fault/recovery, and >=72-hour soak evidence.

Release scope and gates are defined in `docs/RELEASE_PROCESS.md`. The legacy `mhilton7/power-monitor` application is not a supported import source; see `docs/MIGRATION.md`.
