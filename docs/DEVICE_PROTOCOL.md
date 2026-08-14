# Device protocol

Protocol identifier: `pm-protocol/1.0.0`. Breaking changes require a coordinated server and firmware version bump; additive optional fields preserve compatibility.

## Authentication

The canonical signature input is:

```text
PM-HMAC-SHA256-V1
<UPPERCASE METHOD>
<PATH AND CANONICAL QUERY>
<TIMESTAMP>
<NONCE>
<LOWERCASE SHA-256 OF EXACT BODY BYTES>
```

Both repositories consume the same deterministic HMAC/HKDF/canonical-query test vectors. Requests with a wrong protocol, credential, signature, body hash, timestamp window, nonce, device scope, or size fail with typed RFC 9457 problems. The server stores a bounded nonce replay record.

## Heartbeat and live evidence

The default heartbeat is 15 seconds. It reports firmware/protocol, health flags, measurement values with UTC/monotonic evidence, PZEM/storage/network state, backlog/cursors, resource watermarks, command/OTA state, and typed diagnostics. Signed receipt—not ping—sets online state. Invalid/missing values are null, not zero.

The response provides authoritative server time, desired configuration version, acknowledgment/gaps, and only commands owned by that device. Backlog upload cannot starve heartbeat.

## Durable readings

A batch contains at most 500 immutable records and obeys the stricter byte limit. Every record preserves device sequence, interval UTC when trusted, sample completeness/quality, electrical values, cumulative PZEM energy evidence, selected interval energy, and typed flags.

The server deduplicates `(device_id, sequence)`. An identical retry succeeds idempotently; different content at an existing sequence is a critical integrity conflict. Acknowledgment advances only after commit over contiguous sequences plus authenticated permanent-loss ranges and never regresses.

## Commands and OTA

Commands are delivered through heartbeat or bounded poll. State/progress/result posts include the original command and idempotency IDs. The device validates type, capability, not-before/expiry, ownership, and prepare/commit token. OTA download uses the same outbound authenticated HTTPS channel and verifies project, chip, board, compatibility, size, and SHA-256 before boot selection.

Browser clients never use this device protocol and never receive device secrets.
