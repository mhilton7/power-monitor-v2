# History

History is generated only from committed authenticated sensor evidence. It cannot be populated, calibrated, reconciled, scaled, or gap-filled from a utility bill.

Each raw reading is immutable and unique by `(device_id, sequence)`. Normalized intervals reference raw evidence and record selection/quality metadata. Rollups reference their source intervals and algorithm version so they can be recomputed without mutating raw evidence. Duplicate retries do not double count.

Queries support live, today, 24 hours, 7 days, 30 days, billing cycle, and custom UTC ranges; metrics include power, voltage, current, frequency, power factor, energy, and cost. The response reports timezone, aggregation resolution, completeness, missing/unavailable ranges, and rate version when cost is requested.

Missing values are null and chart lines break across gaps. Measured zero is retained. Server aggregation and client decimation bound large ranges. Time-aware ticks use viewport-dependent minimum pixel spacing, auto-skip, and zoom/pan so labels do not overlap; rotation is a last resort. CSV export uses the same permission/scope filters and includes evidence/quality fields without secrets.

DST rules use UTC instants and the configured IANA timezone: no energy is invented in a spring-forward gap and repeated local times in a fall-back hour remain distinct. Rate and billing boundaries split intervals before cost aggregation.

Deletion is an explicit coordinated data-reset workflow. It increments a reset generation and prevents pre-reset microSD records from silently repopulating History while preserving device enrollment, sequence floor, acknowledgment evidence, network configuration, and OTA compatibility.
