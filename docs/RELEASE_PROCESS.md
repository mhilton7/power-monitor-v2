# Release process

## Version and branch discipline

Work occurs on `codex/*` feature branches in logical commits. Pull requests remain draft until mandatory automated gates pass. No force-push, history rewrite, unrelated overwrite, floating production dependency, or secret publication is permitted.

Repository visibility is **public**, matching the verified public reference repository. This is not permission to publish secrets, customer documents, private evidence, or a stable build.

Server tags use semantic `vMAJOR.MINOR.PATCH` (prerelease suffix allowed). Breaking device changes require a coordinated protocol bump in both repositories; otherwise the manifest remains `pm-protocol/1.0.0` and names compatible firmware.

The first eligible publication is `v0.1.0-rc.1`, and only after every automated gate passes. Stable publication remains blocked until physical hardware, TLS, OTA-install/rollback, and soak certification from the actual marked unit passes.

## Gates

1. Clean checkout and exact dependency locks.
2. Backend lint/type/unit/integration, clean and previous-V2 migration, bill-boundary invariance, backup/restore tests.
3. Frontend lint/type/component/accessibility/visual/browser tests.
4. Shared protocol vector/contract tests against the compatible firmware release.
5. Static Compose/hardening plus clean/upgrade/rollback target deployment suite.
6. Dependency review, pip/npm audit, Gitleaks, CodeQL, and filesystem/container vulnerability scans.
7. Firmware host/fault/simulation evidence and machine-readable hardware certification status.

Missing, skipped without an approved reason, stale, or failed evidence blocks stable release.

## Current candidate state

The 2026-08-14 local candidate completed the feasible prepublication gates:
101/105 portable Python tests passed with four expected environment skips;
102/105 role-separated PostgreSQL tests passed with three expected skips; 13
Vitest and 18 production Playwright tests passed; the PDF sandbox, release and
cross-repository contract validators passed; encrypted backup plus automatic
and operator-style isolated restores passed; and the firmware candidate at
`5dea90d91ecd5731b4286a5f67117741aa2ce539` passed 55 host, 36 fault,
63 production-C, 63 sanitizer, and accelerated 120-day simulation gates.
Exact evidence is recorded in `docs/TESTING.md`.

The API, frontend, and backup Docker values in that report are local image IDs,
not registry manifest digests. No signed tag, public GitHub Release, public GHCR
package, anonymous digest resolution, attestation, or generated real-digest
TrueNAS YAML has been produced. The checked-in YAML therefore retains
`UNPUBLISHED_*` sentinels. The target GitHub repositories are currently absent,
`gh` authentication is invalid, and no signing key/tool is registered or
configured; creating an unsigned tag or hand-substituting local image IDs is not
an acceptable fallback. Marked-unit identity/electrical, TLS/HMAC, OTA
install/rollback, outage/recovery, physical-cycle, USB, and continuous 72-hour
soak evidence is also absent. Prerelease publication still requires a signed
tag and the GitHub build/security/public-package/full seven-service smoke gates.
Physical evidence does not block an honestly labeled release candidate after
those nonphysical gates pass, but it independently blocks stable promotion;
local simulation or Docker evidence cannot substitute for it.

## Build and publish

The tagged workflow uses `GITHUB_TOKEN` with job-minimal permissions to build API, frontend, and backup images for the declared platforms. OCI labels include source, semantic version, and full revision. It publishes semantic and commit tags, records registry-reported digests, generates SBOMs, and creates GitHub/Sigstore provenance and SBOM attestations. The source repository must be public, and an anonymous digest lookup must succeed for all three GHCR packages before a GitHub prerelease is assembled. For each package's first publication, an owner may need to set package visibility to public in GitHub Packages and rerun the blocked jobs; no workflow treats authenticated-only access as public.

`scripts/render_truenas_release.py` accepts only semantic version, full revision, and three syntactically valid nonzero digests. It replaces every fail-closed sentinel, verifies exactly seven services, digest pinning, network/port/hardening constraints, and emits `power-monitor-v2-<version>.yaml` plus `release-manifest.json`. `scripts/verify_release_artifacts.py` verifies checksum and invariants.

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
