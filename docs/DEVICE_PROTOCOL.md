# Device protocols

Authentication, control, enrollment, repair, and OTA use
`pm-protocol/1.0.0`. Breaking changes require a coordinated server and firmware
version bump. Stateless measurements use the additive body contract
`pm-telemetry/2.0.0`.

## Authentication

The canonical signature input remains:

```text
PM-HMAC-SHA256-V1
<UPPERCASE METHOD>
<PATH AND CANONICAL QUERY>
<TIMESTAMP>
<NONCE>
<LOWERCASE SHA-256 OF EXACT BODY BYTES>
```

Wrong protocol, credential, signature, body hash, timestamp window, nonce,
device scope, schema, semantics, or size receives an ordinary typed 4xx
problem. TLS chain and hostname verification remain mandatory.

## Stateless telemetry

`POST /api/v1/device/telemetry/v2` carries:

- `telemetry_protocol: pm-telemetry/2.0.0`;
- signed sensor identity, per-boot UUID, and RAM-only unsigned sample number;
- trusted timestamp or null, uptime, decimal-string electrical values or null,
  cumulative PZEM energy or null, PZEM status, firmware version and full build
  ID, time status, RSSI, and bounded command results.

The signed response reports `accepted` or `duplicate`, server receive time,
the exact echoed sample identity, timestamp source, optional cadence settings,
and owned command envelopes. It contains no acknowledgment cursor, missing
prefix, gap list, or backlog. A rejected request is never encoded as a success
status.

## Commands and OTA

Commands remain durable, expiring, idempotent, and device-scoped. OTA uses
authenticated outbound HTTPS and verifies project, target, board, protocol,
configuration, size, semantic upgrade policy, and SHA-256 before changing the
inactive boot slot. Server deployment success additionally requires the same
sensor to report the expected semantic version and full firmware build ID
after reboot.

Browser clients never use these device protocols and never receive device
secrets.
