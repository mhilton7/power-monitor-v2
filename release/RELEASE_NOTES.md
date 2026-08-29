# PowerMeter V2 v0.1.0-rc.29

RC29 carries forward the public RC28 server-owned History, billing presentation,
official-rate catalog, responsive chart, and firmware/deployment lifecycle
work while adding per-user notification dismissal. Alert records, lifecycle
state, and audit evidence remain intact when a user removes a notification. The
firmware runtime remains stateless; this candidate advances its coordinated
release binding without reintroducing storage or backlog behavior. It remains
a release candidate because marked-unit hardware certification is pending.

## RC29 notification controls

- Each authorized user can remove an individual open or acknowledged alert
  from their own notification view without deleting the shared alert record.
- Clear all removes every currently visible notification for that user,
  including stale acknowledged alerts, while preserving resolution, lifecycle,
  and audit evidence for operators and other users.
- Additive migration `20260829_0020` stores only per-user dismissal view state.
  Existing alerts, acknowledgements, silences, and resolution evidence are not
  rewritten or deleted.
- Power History now commits range movement from pointer, mouse, touch, and
  keyboard input. The selected brush geometry and timestamps remain fixed when
  a five-second authenticated-reading refresh arrives; only Reset zoom or
  Resume live returns the brush to the full range.
- Public server and firmware RC28 artifacts remain immutable and are never
  relabeled with the new API contract.

## Public RC28 dashboard and SCE source repairs retained

- Power History freezes one local chart-data snapshot while the range traveller
  is being moved. Per-pixel movement stays inside the chart, the selected range
  is committed once on release, and an arriving authenticated reading cannot
  replace the traveller's data midway through a gesture. Reset zoom and Resume
  live explicitly return to current server data.
- A committed chart range now survives the five-second authenticated-reading
  refresh instead of jumping back to the full live range. Keyboard focus stays
  visible on explicit controls, while selecting the chart itself no longer
  draws a white browser focus border.
- The scheduled official source now starts at SCE's residential
  `time-of-use-plans` page rather than the tiered-rate page. Administrators may
  run a bounded one-time check against a custom official SCE HTTPS URL from
  Settings; exact-host, approved-path, DNS, redirect, TLS, and SSRF protections
  remain enforced.
- The obsolete built-in tiered-page source is disabled without deleting its
  revisions, catalog entries, sync runs, or audit provenance.
- The strict browser schema now accepts both states of
  `effective_date_confirmation_required` and preserves the bounded
  `rate_precision` metadata returned by the official SCE parser. The same
  contract applies to current candidate facts and `diff.after`; known TOU
  component, baseline, eligibility, and enrollment evidence is accepted while
  unknown keys remain rejected.
- RC27 remains an immutable public server and firmware prerelease; its signed
  tags, images, YAML, firmware binary, checksums, and attestations are not moved,
  rewritten, or relabeled.

## Public RC26 baseline retained

- Power History now tracks the active brush selection and formats the selected
  local-time range compactly. Its footer uses a bounded responsive grid so the
  selected range and reading-coverage text cannot overlap after dragging.
- Today's Daily Energy chart uses the server's authoritative calendar-day
  `yesterday` and `today` summaries in the configured IANA timezone, including
  accepted safely recovered PZEM energy. It no longer asks a rolling UTC
  24-hour History bucket to stand in for calendar-day totals, and missing
  readings are never converted to zero.
- Firmware Settings accepts the additive OTA job build-identity evidence
  `target_firmware_build_id` and
  `reported_firmware_build_id_after_reboot`, plus reconciliation
  `artifact_quarantines`. The response remains strict about the typed evidence
  while tolerating future additive lifecycle fields, so the view no longer
  fails when the current server returns those fields.
- Firmware/server RC26, RC25, and RC24 remain immutable public prereleases. RC22 remains
  immutable failed-candidate evidence and is not rewritten or relabeled.

The signed server `v0.1.0-rc.22` tag and run
[`32451170213`](https://github.com/mhilton7/power-monitor-v2/actions/runs/32451170213)
are immutable failed-candidate evidence. Mandatory gates, four image
publications, and anonymous GHCR verification passed, but deployment smoke
failed when a published bill-derived day-sensitive rate had no authoritative
holiday calendar. Assembly was skipped, so RC22 has no server GitHub Release
or generated YAML. Its tag, run, images, and logs are not relabeled as RC23.

## Remaining web application repair

- Official SCE discovery is rooted at the residential TOU page and accepts
  only Schedule D, TOU-D 4–9 PM, TOU-D 5–8 PM, and TOU-D-PRIME from the
  primary page content. Navigation, footer, marketing, solar, comparison,
  FAQ, and cross-family enrichment links cannot become rate candidates.
- The production parser reads the three embedded TOU families from the
  allowlisted page structure and follows only the exact Tiered-plan link.
  Obsolete false candidates are excluded from the active catalog without
  deleting their stored provenance, and rounded public prices remain
  review-only until exact tariff components are verified.
- Billing keeps authenticated measured, cumulative-meter recovered, bounded
  estimated, and unknown energy separate. Tiered totals may remain exact when
  the PZEM cumulative counter safely closes a chart gap; TOU cost remains
  partial when recovered or estimated energy cannot be placed in an exact
  period.
- Mutable utility, baseline, billing-source, estimate, projection, and History
  interval controls are centralized under Settings → Rates & data sources.
  Billing is a read-focused explanation of server calculations and evidence.
- Daily Energy groups accepted intervals by the configured local calendar day
  and labels accepted, recovered, estimated, and unallocated gap energy
  without rewriting History. Chart range selection remains stable across
  refreshes and exposes explicit reset/resume controls.
- Live power charts retain their last successful data while a new authenticated
  reading is fetched, eliminating the full-card flash without fabricating or
  smoothing any reading.
- API, frontend, and backup final images require Alpine OpenSSL 3.5.8-r0, closing
  CVE-2026-14456; the tagged workflow still scans every final image and fails
  on any HIGH or CRITICAL finding.
- The API final image also requires Alpine sqlite-libs 3.53.4-r0, closing
  CVE-2026-11822 and CVE-2026-11824 before publication.
- Diagnostics now distinguishes server receipt time from trusted sensor sample
  time and reports only server-owned stored-History and accepted-sample
  evidence. Additive OTA lifecycle response evidence remains schema-compatible.

## RC22 smoke remediation

- Bill-rate publication now rejects a day-sensitive schedule whose holiday
  treatment requires an authoritative calendar that bill-only evidence cannot
  supply; the official-source workflow remains the route for completing that
  evidence.
- Stored legacy or otherwise unexecutable rate evidence fails closed for only
  the affected interval/account. The worker records safe unpriceable counts,
  continues independent work, and does not invent a price.
- A bounded pricing-scan checkpoint under `PM_LOG_DIR` persists across worker
  restarts, so invalid historical rates cannot starve later intervals; it never
  fabricates a reading, rate, or price.
- Deployment smoke uses an executable all-day synthetic rate fixture, then
  explicitly requires worker health after the evidence probe. Failure evidence
  exposes only allowlisted worker state, completion time, and error code.
- Firmware/server RC21 and firmware RC22 remain immutable public prereleases.
  RC17 remains immutable failed-candidate evidence and is never rewritten.

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
- Additive revision `20260821_0019` makes utility, baseline, estimation, and
  projection controls authoritative under Settings → Rates & data sources.
  Its downgrade refuses to discard customized configuration; accepted
  telemetry, immutable History, published rates, and bill-source boundaries
  remain unchanged.

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

- Server and frontend version: `0.1.0-rc.29`.
- Compatible firmware tag: `v0.1.0-rc.29`, build number `32`.
- Control protocol: `pm-protocol/1.0.0`.
- Stateless telemetry protocol: `pm-telemetry/2.0.0`.
- Alembic head: `20260829_0020`.
- Generated contract-document SHA-256:
  `c79ca4fd97b6ad349231e21468524119b86db96466e269f78cb7afeab70a6e09`.

The tagged server workflow must publish four multi-architecture GHCR indexes,
their registry digests/SBOMs/attestations/scans, the digest-pinned
`power-monitor-v2-v0.1.0-rc.29.yaml`, release manifest, migration/security/test
evidence, and checksums. The firmware prerelease must be published and
independently verified first, then the server compatibility variable must be
set to the exact RC29 firmware tag.

## Deployment boundary

Deploy the server RC29 YAML before applying the firmware RC29 update. Existing
RC21 through RC28 sensors remain protocol-compatible throughout. Automated
tests do not install firmware on physical sensors. No card was formatted and no
NVS namespace was erased while preparing this release.

Stable promotion remains blocked on actual marked-unit electrical identity,
TLS/HMAC, OTA install/rollback, outage/power-cycle/USB recovery, runtime
stack/heap evidence, and a continuous 72-hour hardware soak.
