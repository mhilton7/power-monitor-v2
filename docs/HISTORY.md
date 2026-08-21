# History

History is generated only from committed authenticated PZEM telemetry. It can
never be populated, calibrated, reconciled, scaled, or gap-filled from a bill.

New samples are immutable and unique by
`(sensor_id, boot_id, sample_sequence)`. Identical retries are idempotent;
different content for an existing identity is an integrity conflict. The
server owns active interval buckets, retained History, energy events, and
connection-gap evidence.

Queries support live, today, 24 hours, 7 days, 30 days, billing cycle, and
custom UTC ranges. Missing values are null, measured zero remains zero, and
chart lines break across connection gaps. Trusted sensor time is used only
within the accepted skew; otherwise server receipt time places the sample.

Main service is an explicitly confirmed set of non-overlapping members. Power
and energy may be summed only when the requested branch is valid for that
instant. Voltage, current, frequency, and power factor remain per-sensor.
Revoked or moved membership remains historical topology rather than being
rewritten.

Cumulative PZEM energy may recover energy across a connection gap without
inventing a missing power curve. Counter decreases create reset evidence and
never negative usage. Energy spanning a billing-cycle boundary remains
unresolved until reviewed.

History interval and retention are server settings. Shortening retention
requires exact confirmation and deletes only expired derived History for the
selected home. Immutable samples, cost-linked evidence, rates, audit records,
and identities remain. A downgrade from revision `20260820_0018` fails closed
when catalog/lifecycle evidence cannot be represented; revision 0017 likewise
refuses to remove accepted stateless samples or cutover evidence.
