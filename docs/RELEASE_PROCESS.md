# Release process

## Version and branch discipline

Work occurs on `codex/*` feature branches in logical commits. Pull requests remain draft until mandatory automated gates pass. No force-push, history rewrite, unrelated overwrite, floating production dependency, or secret publication is permitted.

Repository visibility is **public**, matching the verified public reference repository. This is not permission to publish secrets, customer documents, private evidence, or a stable build.

Server tags use semantic `vMAJOR.MINOR.PATCH` (prerelease suffix allowed). Breaking device changes require a coordinated protocol bump in both repositories; otherwise the manifest remains `pm-protocol/1.0.0` and names compatible firmware.

`v0.1.0-rc.16` remains an immutable public server installation authority. The
valid signed server `v0.1.0-rc.4` tag
is historical failed prepublication evidence, not a Release. Hardware execution
confirmed that firmware rc.1 through rc.5 crash in the main stack before
provisioning. Public `v0.1.0-rc.16` is installation evidence for the prior
  durable sensor-backlog architecture. Candidate `v0.1.0-rc.27` retains
`pm-protocol/1.0.0`, adds `pm-telemetry/2.0.0`, and extends the Alembic head to
`20260821_0019`. It retains PostgreSQL telemetry and History ownership, removes
active microSD/backlog behavior from new firmware, preserves
NVS identity/configuration, and adds Main-service History and exact tiered
Billing. Firmware and server rc.16 remain immutable and must not be relabeled.
Firmware rc.17 is immutable failed-candidate evidence because its public
compatibility record omitted the telemetry protocol binding. The exact
  firmware rc.27 metadata and artifacts must be published and independently
  verified before the server rc.27 tag is created; server
publication still requires every automated gate to pass.
Stable publication remains blocked until physical hardware, TLS,
OTA-install/rollback, and soak certification from the actual marked unit
passes.

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

`v0.1.0-rc.12` remains the immutable public server release and is installable
with its own attached eight-service assets and instructions. The signed server
`v0.1.0-rc.2` tag is immutable failed prepublication evidence, not an
installation authority. Tagged run
[`31866197054`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31866197054)
passed the server test/security/migration job, then failed because public
firmware rc.2 declared a stale generated OpenAPI hash. Image publication and
all downstream release jobs were skipped, so no server rc.2 GitHub Release,
authorized GHCR image set, installable YAML, or deployment smoke exists.

Public rc.3 carries the authorized home-scope discovery fix for first-sensor
enrollment and keeps the database at `20260813_0007` and shared protocol at
`pm-protocol/1.0.0`. Its generated OpenAPI SHA-256 is
`7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c`.
It was published only after the distinct coordinated public firmware rc.3
release declared that exact hash and named server rc.3. Public rc.1 and
firmware rc.2 remain historical; rc.2 server publication never occurred.

The signed server rc.4 tag targets the exact audited merge and remains
immutable. Tagged run
[`31893354667`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31893354667)
passed the workflow's named `Mandatory release gates` job, all four
multi-architecture image jobs, and the
anonymous GHCR access gate. Its digest-pinned deployment smoke failed
deterministically when `docker compose start` traversed dependencies and
restarted the completed one-shot initializer. Release assembly was skipped;
there is no server rc.4 GitHub Release, generated rc.4 YAML, or install
authority. Public firmware rc.4 is valid historical evidence for that attempt
and cannot be relabeled as a later target.

Public rc.5 carries forward the no-shell eight-service initializer/stager,
home-isolated APIs and UI, rate-source durability, bill-document non-retention,
and migration chain through `20260815_0011`. It repaired the deterministic rc.4
recovery check by restarting exact captured runtime container IDs without
Compose dependency traversal and recording a fixed allowlisted failure
assertion. Its generated OpenAPI SHA-256 is
`66b4e1cfb0f5a5797dadd9a8783ff0b192ca416d1f4264c135a4e380b2b94591`.

Public rc.16 carries named service branches and the explicitly designated Main
service. Firmware rc.22 remains immutable public evidence. The signed server
rc.22 tag and run
[`32451170213`](https://github.com/mhilton7/power-monitor-v2/actions/runs/32451170213)
are immutable failed-candidate evidence. Mandatory gates, four image
publications, and anonymous GHCR verification passed; deployment smoke then
failed deterministically because a published bill-derived day-sensitive rate
required a missing authoritative holiday calendar. Release assembly was
skipped, so server rc.22 has no GitHub Release or generated YAML.

Candidate rc.27 keeps the server as durable owner of independently accepted
telemetry and active History. Firmware keeps one in-flight and one newest
pending sample in RAM, and a missing sample never blocks a later sample. The UI
removes normal storage/backlog controls; History preserves connection gaps and
cumulative-energy recovery without inventing a power curve. Billing uses Main
service with exact Decimal tier and fixed-charge semantics. RC27 retains the
public RC24 dashboard/OTA corrections and RC23 rate-evaluation hardening, and
completes the restricted SCE catalog, billing quality, diagnostics, Settings,
home selector, and chart interaction repairs. RC24 corrected dashboard day totals, the
History slider footer, and additive OTA lifecycle response compatibility. RC23 rejects
unexecutable holiday-sensitive bill-rate publication, isolates any legacy
unpriceable rate evidence without degrading unrelated worker work, and requires
post-pricing worker health in deployment smoke. Control remains
`pm-protocol/1.0.0`, telemetry is `pm-telemetry/2.0.0`, and the Alembic head is
`20260821_0019`. The generated rc.27 OpenAPI SHA-256 is
`b730b9e200124b2d45da9f59cedf5cf903e9fcca42b8586d6449c689908d7ff6`.
The checked-in YAML retains `UNPUBLISHED_*` sentinels until its tagged workflow
supplies exact registry digests. Rc.27 must pass clean
dependency/backend/PostgreSQL gates, security scans, public package
verification, first-run plus idempotent initializer smoke, checksums, and
attestations. Its explicit migration chain extends to `20260821_0019`;
revision 0019 adds Settings-owned billing calculation configuration and fails
closed rather than discarding customized values on downgrade; revision 0018
adds catalog/lifecycle evidence and fails closed on unsafe
downgrade; revision 0017 remains additive and refuses to delete accepted
stateless telemetry or cutover evidence. The
0008 preflight refuses conflicting immutable ingestion evidence without
deleting or rewriting it. Revision 0011 uses PostgreSQL write locks across its
preflight and guard installation. It enforces database-backed exact-home manual
candidate idempotency, immutable candidate provenance, the legal review paths
`reviewed -> published -> activated` and `reviewed -> rejected`, a unique
natural rate-plan identity with serialized version allocation shared by bill
and SCE publishing, and deterministic non-overlapping assignments with
equal-start rejection. The migration gate selects the most recently
published non-draft same-major public Release other than the current tag, then
requires that selected tag to be semantically older and to have verified
signed-tag ancestry. It fails closed if publication-date ordering selects a
same-major tag that is not older; it does not search past it for a
`latest lower same-major non-draft public release`. For rc.19, public metadata
must select the most recent qualifying immutable public predecessor; failed
rc.2 and non-released rc.4 are never predecessors. For the historical rc.14
candidate, the same selector therefore selects public rc.13; that historical
selector evidence remains unchanged.
Historical rc.3 evidence proved only forward rc.1-to-rc.3 upgrade. Rollback
remains separately unproven and its
smoke record is `not_exercised_github_hosted_smoke`.

The earlier local rc.1 evidence, including 101/105 portable Python tests,
102/105 role-separated PostgreSQL tests, backup/restore exercises, and firmware
host/fault/simulation results, remains historical evidence for that revision
only. It must not be relabeled as rc.3 evidence. The successful portions of the
failed rc.2 run likewise remain rc.2-only evidence. Marked-unit
identity/electrical, TLS/HMAC, OTA install/rollback, outage/recovery,
physical-cycle, USB, and continuous 72-hour soak evidence is still absent.
Physical evidence does not block an honestly labeled release candidate after
those nonphysical gates pass, but it independently blocks stable promotion;
local simulation or Docker evidence cannot substitute for it.

## Build and publish

The tagged workflow uses `GITHUB_TOKEN` with job-minimal permissions to build API, frontend, gateway, and backup images for the declared platforms. OCI labels include source, semantic version, and full revision. It publishes semantic and commit tags, records registry-reported digests, generates SBOMs, and creates GitHub/Sigstore provenance and SBOM attestations. The source repository must be public, and an anonymous digest lookup must succeed for all four GHCR packages before a GitHub prerelease is assembled. For each package's first publication, an owner may need to set package visibility to public in GitHub Packages and rerun the blocked jobs; no workflow treats authenticated-only access as public.

`scripts/render_truenas_release.py` accepts only semantic version, full revision, and four syntactically valid nonzero digests. It replaces every fail-closed sentinel, verifies exactly eight services, exact initializer host mounts/capabilities/no-network gating, runtime secret-directory isolation, digest pinning, network/port/hardening constraints, and emits `power-monitor-v2-<version>.yaml` plus `release-manifest.json`. `scripts/verify_release_artifacts.py` verifies checksum and invariants.

Release assets include manifest, digest-pinned YAML, SBOMs/attestations,
test/security/migration reports, checksums, installation/upgrade/rollback
guides, the tracked Windows SMB staging helper, the auditable initializer
source embedded in the API image, and release notes. The GitHub Release
cross-links the compatible firmware release. Coordinated rc.27 publishes under
a new immutable tag without rewriting rc.24 or any earlier release.

### Release-candidate publication order

Never tag a feature-branch commit. Publish an RC only in this order:

1. Push the `codex/*` branch, open a pull request into `main`, and leave it in
   draft while mandatory CI, security, migration, and review gates run. Merge
   the approved pull request through the protected `main` branch; do not bypass
   review or required checks.
2. Update a clean local `main` and prove it is the exact remote merge commit:

   ```bash
   git fetch --prune origin
   git switch main
   git pull --ff-only origin main
   test "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)"
   test -z "$(git status --porcelain)"
   ```

3. Publish and independently verify the coordinated signed firmware
   `v0.1.0-rc.27` release first. Set the server repository variable
   `COMPATIBLE_FIRMWARE_TAG` to that exact tag and verify its immutable release
   metadata before creating the server tag.
4. With the release signing key and local allowed-signers policy configured,
   create a signed annotated server tag on the audited `main` merge commit,
   verify it locally, prove its target, and only then push the tag:

   ```bash
   release_commit="$(git rev-parse HEAD)"
   git tag -s -m 'PowerMeter V2 0.1.0-rc.27' v0.1.0-rc.27 "$release_commit"
   git verify-tag v0.1.0-rc.27
   test "$(git rev-parse 'v0.1.0-rc.27^{commit}')" = "$release_commit"
   git push origin refs/tags/v0.1.0-rc.27
   ```

The tagged workflow independently requires the pushed ref to resolve to an
annotated tag object, requires GitHub to report a valid cryptographic signature
with complete verification evidence, and requires the signed tag target to be
the exact workflow commit. A lightweight, unsigned, invalid, or retargeted tag
fails at the first mandatory release gate, before test, security, migration, or
publication jobs can run. Do not create or push the tag if any local
verification or prerequisite fails.

## Stable prohibition and promotion

Tagged builds publish as prerelease candidates while hardware certification is
pending. Stable promotion is manual through
`.github/workflows/stable-promotion.yml` and an approval-protected
`stable-release` environment. It promotes the already tested candidate digests
without rebuilding. It requires an actual `pm-hardware-certification/2.0.0`
marked-unit record, schema validation, canonical record hash, matching public
firmware release commit and `firmware.bin` SHA-256, all physical/TLS/HMAC/OTA/
recovery booleans true, unique physical-photo hashes, a passing stateless soak of at
least 72 hours, zero unexplained reboots, identity changes, or configuration losses, every
server report passed, and compatible release links. Simulation cannot satisfy
that gate.

Record repositories/branches/commits/PRs/releases, image names/versions/digests, firmware hash, final YAML, datasets/secrets, test counts, fault/HIL evidence, pending physical work, limitations, migration notes, secret scan, reference preservation, bill-boundary evidence, and traceability in the final report. Never claim a URL, digest, test, deployment, or certification that was not produced.
