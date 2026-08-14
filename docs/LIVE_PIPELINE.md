# Live pipeline

Home's live value and History are deliberately separate evidence paths.

1. The PZEM driver measures at the firmware's one-second default and validates transport/ranges.
2. A signed heartbeat carries the latest measurement plus measurement and receipt timestamps.
3. The API verifies HMAC, replay, time, body limits, device scope, and ranges before updating the bounded latest-heartbeat view.
4. The API emits a same-origin SSE update. The browser uses bounded polling only when SSE cannot be maintained.
5. Freshness resolves to `live`, `waiting`, `stale`, `offline`, `unavailable`, `invalid`, or `needs_attention`. A future timestamp or invalid range never becomes live.

Separately, the firmware aggregates a durable interval, appends it to microSD, and uploads it with a monotonic sequence. After the server commits the immutable raw record and derived interval transaction, it acknowledges the sequence and History becomes queryable. A live heartbeat alone never creates History or energy.

Outage behavior:

- Wi-Fi/server outage: one-second measurement and durable microSD logging continue; signed heartbeats resume automatically; bounded batches backfill between heartbeat deadlines.
- PZEM outage: heartbeat and storage diagnostics continue, values are missing, and no energy is fabricated.
- microSD outage/full: live measurement and heartbeat continue; History gaps are explicitly reported; unsynchronized data is never deleted for space.
- Browser disconnect: server ingestion continues; reconnection obtains current live state and committed History.

Every UI aggregate names its scope. Parent/child circuits are not blindly summed; voltage, frequency, and power factor are never summed.
