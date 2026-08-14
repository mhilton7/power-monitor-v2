# Alerts, logs, and diagnostics

Typed alert rules cover sensor offline, delayed heartbeat, reading backlog, PZEM unavailable, microSD missing/read-only/nearly-full/corrupt segment, time untrusted, TLS validation failure, repeated Wi-Fi failure, OTA failed/rolled back, rate source changed, rate sync failed, backup failed, and restore test failed.

An alert is backed by evidence and state transitions: observation → debounce threshold → open → acknowledged or silenced/maintenance → resolved. Repeated observations update the same alert rather than flooding. Acknowledgment does not erase evidence; silence has actor/scope/expiry; maintenance windows are bounded. Recovery evidence resolves the alert.

Application and gateway logs are JSON with UTC timestamp, severity, typed event code, message template, service/version, correlation ID, and relevant device/command/sync/rate-source IDs. Values are allowlisted/redacted; passwords, tokens, cookies, HMAC/OTA/encryption keys, customer bill fields, retained PDFs, OCR text, and authorization headers never enter normal logs. Rotation and default retention are 90 days.

Downloadable diagnostics bundles include only allowlisted health/configuration metadata, recent redacted event summaries, release/protocol versions, resource watermarks, and exact evidence identifiers. The bundle manifest lists every member's size and SHA-256 plus an archive SHA-256. Generation fails closed if redaction/schema validation fails. Bill artifacts and secret files are excluded.

Operators use correlation IDs to join a device heartbeat, reading batch, command, rate sync, and backup event without exposing credentials. See `docs/OPERATIONS.md` for response playbooks.

Operational views are home-scoped. `/system/health` selects sensors, open-alert counts, and the latest rate-sync run only from the authenticated actor's homes. New rate-sync runs carry their initiating home. Downloadable diagnostics include an application-log row only when it has at least one authorized home, device, command, or sync association and every populated association resolves inside the actor's scope. Unowned rows and rows with conflicting cross-home identifiers fail closed and are omitted.
