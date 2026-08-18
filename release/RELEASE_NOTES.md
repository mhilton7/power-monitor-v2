# PowerMeter V2 v0.1.0-rc.16

PowerMeter V2 is a central, authenticated PZEM-004T monitoring system paired
with the independent
[`power-monitor-sensor-headless`](https://github.com/mhilton7/power-monitor-sensor-headless)
firmware. The shared device contract remains `pm-protocol/1.0.0`.
Authenticated PZEM evidence remains the only source for live measurements,
History, energy, reading coverage, forecasts, and usage-based cost. Utility-bill
PDFs remain rate-source documents only.

This source file is the release-body input for candidate `v0.1.0-rc.16`. This
source copy alone is not publication evidence. Installation is authorized only
after the signed tagged workflow publishes the complete checksummed and
attested asset set, digest-pinned YAML, public multi-architecture images,
deployment evidence, and its exact coordinated firmware release.

## Coordinated firmware boundary

Firmware `v0.1.0-rc.15` remains immutable historical evidence. It must not be
overwritten, retagged, or relabeled as RC16. Before a server RC16 tag is
created, the independent firmware repository must create and publish distinct
RC16 metadata and artifacts that name server `v0.1.0-rc.16` and bind the exact
generated server OpenAPI SHA-256:

`8c6d3d73f7bfaa4bd34b4451c860b4199426e556cba1f6f9a48374ea22049c24`

The server release workflow requires `COMPATIBLE_FIRMWARE_TAG` to equal the
server tag, verifies the public firmware tag and release, checks every asset,
and validates the cross-repository compatibility record. This root/server
identity pass does not modify the nested firmware repository or claim a
physical OTA was performed.

## Reliability and synchronization

- Ingestion remains transactional, per immutable sensor ID, idempotent by
  `(device_id, sequence)`, and acknowledgement advances only over durable
  readings or accepted authenticated permanent-loss ranges.
- Late backlog readings invalidate only mutable selected-cost pointers at and
  after the late timestamp. The worker rebuilds chronological tier progression
  while immutable readings, rate versions, cost runs, and cost rows remain
  unchanged.
- Settings diagnostics expose the server acknowledgement, SD sequence range,
  queued count, missing-prefix state, latest accepted reading, permanent-loss
  evidence, and a clearly labeled heartbeat-derived queue drain rate.
- Device-only request attempt details that are not present in
  `pm-protocol/1.0.0` remain explicitly unavailable; the server does not invent
  batch bytes, HTTP results, or failed attempts.
- Live browser counters and timelines advance from browser time without adding
  one API request per second.

## Service branches and Main service

- The existing verified circuit model is generalized into named service
  branches with description, purpose, home-total designation,
  non-overlap confirmation, timestamps, membership management, and audit
  records.
- Additive Alembic revision `20260817_0016` designates only an unambiguous
  existing verified aggregate with at least two active members as
  `Main service`. It reads existing membership and never hard-codes sensor
  display names.
- One home may have only one designated home-total branch. A home total requires
  at least two active, explicitly confirmed non-overlapping sensors. Cross-home,
  revoked, duplicate, and protected-member moves fail closed.
- Moving membership is explicit and audited. The current home-total branch
  cannot be deleted or silently lose a required member; an unused branch must
  have no members before deletion.
- Existing sensor IDs, home relationships, raw readings, normalized intervals,
  and historical rate evidence are preserved. Revoked branch members remain in
  historical topology so their absence becomes a visible gap instead of an
  undercount.

## History and live totals

- Dashboard and History default to the designated `Main service` branch while
  preserving explicit individual-sensor selection.
- Whole-home power and interval energy sum only explicitly confirmed members.
  Voltage, current, frequency, and power factor are never summed or averaged
  into a misleading branch value.
- A whole-home bucket is complete only when every required member is present.
  Missing members produce a gap; measured zero remains zero.
- Valid individual sensor points render even when the surrounding range has low
  coverage. Durable History continues to use accepted intervals only; live
  heartbeats are never copied into History.

## Billing and SCE tier behavior

- Billing identifies the exact designated service branch used for whole-home
  usage and returns a plain current-rate-plan and current-billing-cycle summary.
- SCE DOMESTIC regression values remain exact Decimal evidence: Tier 1
  `$0.30863/kWh`, Tier 2 `$0.40962/kWh`, daily service charge `$0.769/day`, and
  summer allowance `19.3 kWh` per billing day.
- The dynamic Tier 1 thresholds are exactly 540.4 kWh for 28 days, 579.0 kWh
  for 30 days, and 598.3 kWh for 31 days. Exactly 579.0 kWh remains Tier 1;
  579.1 kWh assigns only 0.1 kWh to Tier 2.
- The 951 kWh source-bill regression remains 579 kWh in Tier 1 plus 372 kWh in
  Tier 2. Energy charges plus 30 daily charges equal `$354.145410`, rounded to
  `$354.15`.
- Current tier and tier remaining are not confirmed until the designated
  branch has 100 percent billing-cycle reading coverage. Saved partial usage is
  labeled as partial while readings are syncing.
- Whole-home estimates include the account-level fixed charge once. Individual
  sensor and non-home service-branch estimates remain energy-charge-only.
- Published rate versions and their provenance remain immutable. Original PDF
  bytes are never persisted; disposable extraction working records remain
  separately deletable under their existing protections.

## Interface and build identity

- Dashboard, History, Billing, Settings, rate-update, and diagnostics surfaces
  use plain-language labels such as `Saved sensor readings`, `Reading coverage`,
  `Current rate plan`, and `Service branch`; internal identifiers remain under
  administrator technical details.
- Frontend and backend expose semantic version, revision, build time, image
  digest, static asset identifier, protocol compatibility, database migration
  state, and per-sensor firmware identity without exposing secrets.
- Browser assets remain content-hashed and the deployment manifest binds the
  frontend, backend, database migration, images, and coordinated firmware
  release.
- The additive API remains backward compatible. Existing endpoint and response
  fields are preserved while service-branch, billing-summary, and diagnostics
  fields are added.

## Database and data-integrity boundary

Alembic head is `20260817_0016`. Revision 0016 is non-destructive and safe for
populated installations: it adds service-branch metadata and constraints,
preserves every existing row, and migrates only one unambiguous safe aggregate
per home. Ambiguous homes remain undesignated for administrator review.

Earlier frozen migrations and direct-database guards continue to protect raw
reading immutability, permanent-loss evidence, rate provenance, candidate
lifecycle, assignment ranges, original-bill non-retention, microSD capacity
coherence, and OTA deployment history. Do not bypass a guard or delete evidence
to force an upgrade.

Keep all existing application secrets, database credentials, backup encryption
key, TLS material, datasets, sensor identities, and accepted readings. Complete
a verified encrypted backup, isolated restore test, and recursive ZFS snapshot
before applying RC16.

## Release contents and install authority

A successfully published RC16 candidate includes:

- four immutable multi-architecture GHCR images referenced by registry digest;
- `power-monitor-v2-v0.1.0-rc.16.yaml` and `release-manifest.json`;
- checksums, attestations, SBOMs, vulnerability results, dependency and test
  reports, migration evidence, and deployment-smoke evidence;
- installation, secrets/TLS, dataset/ACL, backup/restore, upgrade, and rollback
  instructions; and
- the exact coordinated public firmware `v0.1.0-rc.16` release whose
  compatibility record declares OpenAPI SHA-256
  `8c6d3d73f7bfaa4bd34b4451c860b4199426e556cba1f6f9a48374ea22049c24`.

The checked-in TrueNAS template intentionally contains `UNPUBLISHED_*`
sentinels and is not installable. Use only the generated YAML attached to the
signed release and verify `SHA256SUMS` plus attestations before installation.
Do not use a floating image tag, local Docker image ID, or files from another
release.

## Upgrade, rollback, and certification

The release migration report proves only a forward upgrade. It does not prove
older binaries can use a database touched by RC16. Application-only rollback
is not authorized. Rollback requires a separately validated restore or clone
of the matching pre-upgrade snapshot or verified backup paired with the exact
older release assets. GitHub-hosted smoke records rollback as
`not_exercised_github_hosted_smoke`.

This remains a prerelease candidate. Hardware status is honestly `pending`.
Marked-unit electrical identity, physical TLS/HMAC behavior, OTA installation
and rollback, outage/recovery cycling, and a continuous measured soak of at
least 72 hours must produce schema-valid machine evidence before stable
promotion can open. Simulation, host tests, CI, or candidate publication cannot
substitute for those physical results.
