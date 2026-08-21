# PowerMeter V2 v0.1.0-rc.22

RC22 completes the server-owned History, billing presentation, official-rate
catalog evidence, responsive chart, and firmware/deployment lifecycle work for
the coordinated stateless-telemetry architecture. The already-correct firmware
runtime remains stateless; this candidate updates its operator evidence and
release binding without reintroducing storage or backlog behavior. It remains
a release candidate: marked-unit hardware certification is still pending.

Firmware and server RC21 remain immutable public prereleases. RC22 retains the
protocols, NVS configuration, TLS/HMAC controls, and stateless
latest-value-wins memory bounds described below. RC17 also remains immutable
failed-candidate evidence and is never moved or rewritten.

## Stateless sensor telemetry

- Firmware no longer links, mounts, reads, writes, repairs, verifies, or
  formats microSD storage. The inserted cards are left untouched.
- Firmware keeps at most one in-flight reading and one newest pending reading
  in RAM. A newer unsent reading replaces the older pending reading.
- Each current reading is posted independently to
  `POST /api/v1/device/telemetry/v2` using `pm-telemetry/2.0.0`.
- Authentication, commands, enrollment, recovery, and OTA retain
  `pm-protocol/1.0.0`, strict TLS hostname/chain verification, directional
  HMAC, nonce replay protection, signed responses, and immutable OTA digests.
- A missing sample never blocks a later sample. Wi-Fi and server failures use
  separate bounded retry paths with jitter and create honest server History
  gaps rather than a persistent device backlog.
- Sensor identity, Wi-Fi settings, CA trust, credentials, provisioning, and
  OTA metadata remain in the existing NVS schema. NVS is never erased and is
  not written once per telemetry sample.

## Server History and data integrity

- Additive Alembic revision `20260818_0017` stores immutable telemetry samples
  under `(sensor_id, boot_id, sample_sequence)`, durable active History
  buckets, live state, telemetry settings, cutover records, cumulative-energy
  events, and explicit billing-cycle adjustment evidence.
- Sensor time is used only when trusted and within the existing skew bound;
  otherwise server receive time places the sample without rejecting it.
- History interval and retention are server settings. Shorter retention needs
  an exact administrator confirmation and deletes only expired derived History
  in the selected home; immutable samples and cost-linked evidence remain.
- PZEM cumulative energy can recover total energy across a connection gap
  without inventing a missing power curve. Counter decreases produce reset
  evidence and never negative energy. A gap crossing a billing-cycle boundary
  remains unresolved until reviewed.
- The migration preserves all existing accepted readings, History, rate plans,
  users, homes, devices, firmware evidence, and audit records. Its downgrade
  refuses to remove accepted stateless telemetry or cutover evidence.
- Additive revision `20260820_0018` adds normalized official-rate catalog
  evidence plus explicit firmware-release and deployment lifecycle state. It
  preserves prior History, rates, artifacts, OTA results, and audit evidence;
  destructive lifecycle actions require authorization, exact confirmation,
  transactional revalidation, and an audit tombstone.

## Main service and Billing

- The explicitly confirmed `Main service` branch contains the two existing
  non-overlapping sensors, is the whole-home total, and is the billing source.
  New sensors are never added automatically.
- Whole-home power and interval energy are additive. Voltage, current,
  frequency, and power factor remain per-sensor values and are not summed.
- Dashboard and History default to Main service, show partial live totals
  explicitly, preserve measured zero, and leave outages as chart gaps.
- Billing uses exact Decimal arithmetic: Tier 1 `$0.30863/kWh`, Tier 2
  `$0.40962/kWh`, daily service charge `$0.769/day`, and a summer allowance of
  `19.3 kWh` per billing day. A 30-day cycle therefore has a `579.0 kWh`
  Tier 1 threshold; exactly 579.0 remains Tier 1 and Tier 2 starts above it.
- The canonical 951 kWh fixture splits into 579 kWh Tier 1 and 372 kWh Tier 2:
  `$178.69677 + $152.37864 + $23.07000 = $354.14541`, displayed as `$354.15`.
- Full-cycle projection requires at least 24 reliable hours, applies the fixed
  service charge once, and discloses confidence, missing readings, unresolved
  counter resets, and unresolved cross-cycle gap energy.

## Interface

- Normal sensor UI removes microSD status, capacity, backlog progress,
  acknowledgement cursors, missing-prefix state, Format SD, and Sync Backlog.
- Legacy storage fields may be null or omitted for stateless sensors without
  preventing Dashboard, device-detail, or diagnostics rendering.
- Sensor health shows the latest accepted reading, PZEM health, server delivery,
  and firmware identity. Reboot and signed OTA remain.
- Settings supports service-branch management plus telemetry cadence, History
  interval, and retention. History shows connection-gap ranges and recovered
  energy separately from the power curve.
- Billing presents Current Rate Plan, Current Billing Cycle, Tier Breakdown,
  and Cost Summary in plain language on desktop and mobile.
- Power charts use measured `W`/`kW` axes with sufficient label width, display
  browser-local PST/PDT timestamps, advance Live labels with one shared browser
  timer, and leave missing periods as unshaded disconnected gaps.
- Firmware releases and terminal deployments can be archived and restored.
  Permanent deletion is available only when current, active, queued, pending,
  sensor-reported, shared-artifact, and rollback protections all pass.
- Official SCE discovery follows a bounded allowlisted public-source crawl.
  Catalog readiness is true only when every in-scope discovered document was
  parsed or explicitly excluded; a failed check retains last-known-good data.
- Utility-bill PDFs remain rate-source documents only. Their usage, readings,
  totals, identity, addresses, accounts, meter identifiers, balances, and
  payments are discarded; original bytes/full OCR text are never persisted.

## Release binding

- Server and frontend version: `0.1.0-rc.22`.
- Compatible firmware tag: `v0.1.0-rc.22`, build number `25`.
- Control protocol: `pm-protocol/1.0.0`.
- Stateless telemetry protocol: `pm-telemetry/2.0.0`.
- Alembic head: `20260820_0018`.
- Generated contract-document SHA-256:
  `f15e5429ca0333dbf5f1defeef01197d8a21d2bc9e684c78463f44e279b03123`.

The tagged server workflow must publish four multi-architecture GHCR indexes,
their registry digests/SBOMs/attestations/scans, the digest-pinned
`power-monitor-v2-v0.1.0-rc.22.yaml`, release manifest, migration/security/test
evidence, and checksums. The firmware prerelease must be published and
independently verified first, then the server compatibility variable must be
set to the exact RC22 firmware tag.

## Deployment boundary

Deploy the server RC22 YAML before applying the firmware RC22 update. Existing
RC21 sensors remain protocol-compatible throughout. Automated
tests do not install firmware on physical sensors. No card was formatted and no
NVS namespace was erased while preparing this release.

Stable promotion remains blocked on actual marked-unit electrical identity,
TLS/HMAC, OTA install/rollback, outage/power-cycle/USB recovery, runtime
stack/heap evidence, and a continuous 72-hour hardware soak.
