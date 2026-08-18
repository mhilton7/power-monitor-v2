# Live pipeline

Home, History, energy, and Billing now use one server-owned telemetry path.

1. The PZEM driver samples and validates CRC, address, function, and ranges.
2. Firmware retains one in-flight request and only the newest pending sample.
3. It posts a signed `pm-telemetry/2.0.0` body to
   `POST /api/v1/device/telemetry/v2` over verified HTTPS.
4. The API verifies `pm-protocol/1.0.0` authentication, replay, size, device
   scope, timestamps, and values, then atomically records the immutable sample
   under `(sensor_id, boot_id, sample_sequence)`.
5. The server updates live state, active History buckets, energy/reset/gap
   evidence, and emits a same-origin SSE update.
6. The browser uses SSE with bounded query refresh fallback. It never connects
   directly to a sensor.

Each sample is accepted independently. An outage may lose the older pending
sample, but cannot stop a later sample from being accepted. History displays
that time as a gap; it is never silently interpolated.

Outage behavior:

- Wi-Fi or server outage: measurements continue; retry paths are separately
  bounded and the newest pending sample replaces an older unsent sample.
- PZEM outage: signed status continues with null electrical values; no energy
  or zero reading is fabricated.
- Power loss: the next boot uses a new boot UUID and restarts its RAM-only
  sample number; the server preserves both sessions independently.
- Browser disconnect: server ingestion continues and reconnection loads the
  current live state plus persisted History.

The firmware never mounts, reads, writes, repairs, verifies, or formats a
microSD card. Existing cards are left untouched.
