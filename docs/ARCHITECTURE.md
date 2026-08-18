# Architecture

PowerMeter V2 separates measurement authority, transport, presentation, rate
sources, and cost calculation so no utility document can become electrical
evidence.

```text
PZEM-004T -> headless ESP32-S3 -- authenticated outbound HTTPS --> Caddy
                                                                   |
                                                                   v
                                                    FastAPI -> PostgreSQL
                                                        |          ^
                                                        v          |
                                                      worker ------+
                                                        |
                                                        v
                                              same-origin React browser

Official SCE source or uploaded SCE bill PDF
                 | reusable prices/rules only
                 v
       reviewed immutable rate version
                 |
Authenticated server-owned telemetry + rate version -> estimated cost
```

## Trust boundaries

- PZEM evidence, device identity, TLS, directional HMAC, a boot UUID, a
  boot-local sample number, and the server commit establish measurement
  evidence. New telemetry is immutable and unique by
  `(sensor_id, boot_id, sample_sequence)`.
- The sensor makes outbound HTTPS requests only. It has no runtime web server.
  Browser code never receives device credentials or connects to a sensor.
- `pm-protocol/1.0.0` remains the authentication, command, enrollment,
  recovery, and OTA protocol. Stateless readings use the additive
  `pm-telemetry/2.0.0` body contract.
- Caddy is the only LAN listener. PostgreSQL is the durable owner of telemetry,
  History, energy events, billing evidence, rates, commands, and audit data.
- PDF extraction is a confined rate-only workflow. Original PDF bytes and full
  OCR text are released after parsing and never enter persistent storage.

## Stateless sensor behavior

Firmware measures at the configured cadence and keeps only one request in
flight plus the newest pending sample in RAM. A newer unsent sample replaces
the older pending sample. Missing delivery therefore creates an explicit
server History gap but never blocks a later reading.

Firmware neither mounts nor accesses microSD. Existing cards are left
untouched. Enrollment identity, Wi-Fi, CA trust, directional credentials, and
OTA state remain in the existing NVS schema; NVS is not erased and is not
written once per telemetry sample.

## Time, missing data, aggregation, and money

UTC is authoritative for storage. The configured IANA timezone controls SCE
schedule and billing boundaries. Trusted sensor time is used only within the
accepted skew; otherwise server receipt time places the sample.

Missing values remain null and measured zero remains zero. Main service can
sum only explicitly verified, non-overlapping branch members. Power and energy
are additive; voltage, current, frequency, and power factor are per-sensor and
are never summed. One-CT devices default to `energy_only`.

Money uses `Decimal`/PostgreSQL `NUMERIC` and rounds only at presentation
boundaries. Every estimate retains its immutable rate version and measurement
evidence.

## Reliability and release boundary

The server accepts each current sample independently and idempotently. Wi-Fi
and server failures use separate bounded backoff with jitter. Commands remain
durable and signed; OTA still verifies exact compatibility, image size, and
SHA-256 before boot selection.

Release candidates may be automated and published while physical evidence is
pending, but stable promotion requires marked-unit electrical, TLS/HMAC, OTA,
recovery, resource, and 72-hour soak evidence. Automated tests never install
firmware on physical sensors.
