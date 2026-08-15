# Release process

## Version and branch discipline

Work occurs on `codex/*` feature branches in logical commits. Pull requests remain draft until mandatory automated gates pass. No force-push, history rewrite, unrelated overwrite, floating production dependency, or secret publication is permitted.

Repository visibility is **public**, matching the verified public reference repository. This is not permission to publish secrets, customer documents, private evidence, or a stable build.

Server tags use semantic `vMAJOR.MINOR.PATCH` (prerelease suffix allowed). Breaking device changes require a coordinated protocol bump in both repositories; otherwise the manifest remains `pm-protocol/1.0.0` and names compatible firmware.

`v0.1.0-rc.1` is the published prior candidate. The current server candidate is
`v0.1.0-rc.2`, and it is eligible for publication only after every automated
gate passes with the coordinated firmware `v0.1.0-rc.2`. Stable publication
remains blocked until physical hardware, TLS, OTA-install/rollback, and soak
certification from the actual marked unit passes.

## Gates

1. Clean checkout and exact dependency locks.
2. Backend lint/type/unit/integration, clean and previous-V2 migration, bill-boundary invariance, backup/restore tests.
3. Frontend lint/type/component/accessibility/visual/browser tests.
4. Shared protocol vector/contract tests against the compatible firmware release.
5. Static Compose/hardening plus clean/forward-upgrade target deployment suite;
   rollback requires a separate validated restore/cutover against the matching
   pre-upgrade database.
6. Dependency review, pip/npm audit, Gitleaks, CodeQL, and filesystem/container vulnerability scans.
7. Firmware host/fault/simulation evidence and machine-readable hardware certification status.

Missing, skipped without an approved reason, stale, or failed evidence blocks stable release.

## Current candidate state

`v0.1.0-rc.1` is the retained public installation authority until rc.2 is
published, but its images are not authorized against a database touched by
rc.2. The rc.2 source candidate adds authorized home-scope discovery for
first-sensor enrollment, with focused backend isolation tests, 16 passing
Vitest tests, and 19 passing production Playwright tests. It keeps the database
at `20260813_0007` and the shared protocol at `pm-protocol/1.0.0`.

No rc.2 GitHub Release, installable YAML, or authoritative rc.2 GHCR digest is
claimed by source preparation. The checked-in YAML retains `UNPUBLISHED_*`
sentinels. Rc.2 publication still requires the coordinated firmware rc.2 tag,
clean dependency and backend/PostgreSQL gates, security scans, public package
verification, the full seven-service digest-pinned smoke, checksums, and
attestations. The prior-version migration gate proves only forward rc.1-to-rc.2
upgrade; it does not test rc.1 binaries against the post-upgrade database.
Rollback compatibility remains unproven. The GitHub-hosted smoke records
rollback as `not_exercised_github_hosted_smoke`; any rc.1 recovery requires a
separate validated restore/cutover using the matching pre-upgrade database
snapshot or backup.

The earlier local rc.1 evidence, including 101/105 portable Python tests,
102/105 role-separated PostgreSQL tests, backup/restore exercises, and firmware
host/fault/simulation results, remains historical evidence for that revision
only. It must not be relabeled as rc.2 evidence. Marked-unit
identity/electrical, TLS/HMAC, OTA install/rollback, outage/recovery,
physical-cycle, USB, and continuous 72-hour soak evidence is still absent.
Physical evidence does not block an honestly labeled release candidate after
those nonphysical gates pass, but it independently blocks stable promotion;
local simulation or Docker evidence cannot substitute for it.

## Build and publish

The tagged workflow uses `GITHUB_TOKEN` with job-minimal permissions to build API, frontend, gateway, and backup images for the declared platforms. OCI labels include source, semantic version, and full revision. It publishes semantic and commit tags, records registry-reported digests, generates SBOMs, and creates GitHub/Sigstore provenance and SBOM attestations. The source repository must be public, and an anonymous digest lookup must succeed for all four GHCR packages before a GitHub prerelease is assembled. For each package's first publication, an owner may need to set package visibility to public in GitHub Packages and rerun the blocked jobs; no workflow treats authenticated-only access as public.

`scripts/render_truenas_release.py` accepts only semantic version, full revision, and four syntactically valid nonzero digests. It replaces every fail-closed sentinel, verifies exactly seven services, digest pinning, network/port/hardening constraints, and emits `power-monitor-v2-<version>.yaml` plus `release-manifest.json`. `scripts/verify_release_artifacts.py` verifies checksum and invariants.

Release assets include manifest, digest-pinned YAML, SBOMs/attestations, test/security/migration reports, checksums, installation/upgrade/rollback guides, and release notes. The GitHub Release cross-links the compatible firmware release.

## Stable prohibition and promotion

Tagged builds publish as prerelease candidates while hardware certification is
pending. Stable promotion is manual through
`.github/workflows/stable-promotion.yml` and an approval-protected
`stable-release` environment. It promotes the already tested candidate digests
without rebuilding. It requires an actual `pm-hardware-certification/1.0.0`
marked-unit record, schema validation, canonical record hash, matching public
firmware release commit and `firmware.bin` SHA-256, all physical/TLS/HMAC/OTA/
recovery booleans true, unique physical-photo hashes, a passing soak of at
least 72 hours, zero unexplained reboots, zero sequence regressions, every
server report passed, and compatible release links. Simulation cannot satisfy
that gate.

Record repositories/branches/commits/PRs/releases, image names/versions/digests, firmware hash, final YAML, datasets/secrets, test counts, fault/HIL evidence, pending physical work, limitations, migration notes, secret scan, reference preservation, bill-boundary evidence, and traceability in the final report. Never claim a URL, digest, test, deployment, or certification that was not produced.
