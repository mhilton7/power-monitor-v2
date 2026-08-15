# PowerMeter V2 v0.1.0-rc.3

PowerMeter V2 is a greenfield central server coordinated with the independent
[`power-monitor-sensor-headless`](https://github.com/mhilton7/power-monitor-sensor-headless)
firmware repository through the unchanged `pm-protocol/1.0.0`. Authenticated
PZEM-004T sensor evidence is the only source of live measurements, History,
energy, forecasts, completeness, and usage-based cost. Utility-bill PDFs remain
rate-source documents only.

This file is the release-body input in source control; its presence alone does
not prove a release workflow ran. The public prerelease and its attached
evidence are authoritative. The tagged workflow may publish `v0.1.0-rc.3` only
after all required nonphysical gates pass, the coordinated public firmware
prerelease and contracts verify, every GHCR digest resolves anonymously, and
the complete release artifact set is checksum-valid and attested.

## Change from public v0.1.0-rc.1

The Sensors settings page can now create the first enrollment token before any
device exists. `GET /api/v1/devices` returns only the authenticated actor's
authorized home scopes. The browser uses the scope automatically only when
exactly one exists, requires an explicit UUID-disambiguated choice when
multiple scopes exist, and remains disabled when none exist. Token creation
still rechecks `user_home_scopes` on the server; browser state never grants
authority or guesses a home from an existing sensor. The browser workflow
requires `sensors.view` and `sensors.enroll`, not billing access.

No device request, response, HMAC, enrollment-token, or firmware storage
contract changed. The shared identifier remains `pm-protocol/1.0.0`, and the
database remains at Alembic revision `20260813_0007`; rc.3 adds no migration.

Rc.3 also repairs release coordination without changing runtime behavior. The
release migration gate now chooses the latest same-major, non-draft public
GitHub Release from authenticated GitHub release metadata, requires that exact
tag in the checkout, and archives it. It cannot mistake an unpublished signed
tag for an installable predecessor. The generated rc.3 OpenAPI document has
SHA-256 `7caada9c6295f4c201fd7ce7d383822e6b5785a960022de8355e3b6acc9a4e2c`;
the coordinated firmware rc.3 contract must declare that exact digest.

## Immutable rc.2 failure history

The signed server tag `v0.1.0-rc.2` and failed release run
[`31866197054`](https://github.com/mhilton7/power-monitor-v2/actions/runs/31866197054)
are retained as immutable prepublication evidence. That run passed the server
test, security, and forward-migration gate, then failed cross-repository
validation because firmware rc.2 declared a stale
`power-meter-v2.openapi.json` hash. Image publication and every downstream job
were skipped. There is no server rc.2 GitHub Release, authorized GHCR image set,
digest-pinned TrueNAS YAML, deployment smoke, or release asset set. Do not move
or reuse the rc.2 tag, and do not present its partial workflow artifacts as a
published release.

The signed public firmware rc.2 prerelease remains historical evidence only.
It names server rc.2 and is not the coordinated firmware for this server rc.3
candidate. Server rc.3 publication requires a distinct signed public firmware
rc.3 release with the exact current contract hashes. Until rc.3 publishes,
server rc.1 remains the installation authority.

## Upgrade and rollback

An installed v0.1.0-rc.1 system upgrades in place by downloading and verifying
the complete rc.3 release, stopping the app, running rc.3 `prepare-host.sh`, and
replacing the complete TrueNAS Custom Config with the digest-pinned rc.3 YAML.
Keep the existing ZFS datasets, application secrets, database credentials,
backup key, TLS certificate/key, and trusted CA unchanged. Do not recreate
datasets, rotate secrets, or update only one image.

v0.1.0-rc.1 is the prior public V2 candidate. The rc.1-to-rc.3 migration report
proves only that an rc.1 database can upgrade forward to rc.3. The absence of a
new Alembic revision does not prove that rc.1 binaries support a database touched
by rc.3, and the report does not authorize swapping the rc.1 YAML or images
back onto the current database. Rollback compatibility remains unproven. An
rc.1 image/YAML rollback is not authorized unless a separate release-specific
recovery test validates restoring or cloning the matching pre-upgrade database
snapshot or verified backup and pairing rc.1 with that restored database. The
GitHub-hosted deployment smoke performs a clean deployment and restarts but
does not exercise rollback; its evidence records
`not_exercised_github_hosted_smoke`. Follow `ROLLBACK.md` and do not report
rollback as passed without that separate execution evidence.

## Release contents and firmware pairing

A successfully published candidate includes:

- immutable multi-architecture API, frontend, gateway, and backup images in
  GHCR, each referenced by registry-reported SHA-256 digest;
- a complete generated `power-monitor-v2-v0.1.0-rc.3.yaml` suitable for TrueNAS
  **Apps > Install via YAML**;
- `release-manifest.json`, per-image records, SPDX SBOMs, security results,
  test/migration/deployment reports, checksums, and GitHub attestations;
- `Caddyfile`, `postgres-init-roles.sh`, the checked `prepare-host.sh`, and
  complete install, dataset/ACL, secret/TLS, first-run, backup/restore, upgrade,
  and rollback guides;
- the coordinated public `power-monitor-sensor-headless` v0.1.0-rc.3 identity
  and explicit hardware-certification status.

Server v0.1.0-rc.3 must be paired with the coordinated firmware v0.1.0-rc.3
release required by the tagged workflow. That pairing retains
`pm-protocol/1.0.0`; it is not evidence that physical hardware passed.

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
