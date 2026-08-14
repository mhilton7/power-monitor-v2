# Device commands

Commands are durable database records returned only to their owning device through authenticated outbound heartbeat/poll responses.

States are `queued`, `delivered`, `accepted`, `running`, `succeeded`, `failed`, `expired`, `cancelled`, `superseded`, `awaiting_reboot`, `awaiting_heartbeat`, and `rolled_back`. The device reports real monotonic progress and a typed result; the UI does not claim success before authenticated completion evidence.

Supported types: reboot, maintenance sleep, sync now, diagnostics/network/meter/storage self-tests, storage format prepare/commit, configuration apply, credential rotation, OTA install, and data reset prepare/commit/cancel.

Each record includes command/device/issuer IDs, UTC issue/not-before/expiry, attempt, idempotency key, required capability, bounded payload, state/progress/result, and originating audit ID. Server and device enforce ownership, permission, capability, expiry, and idempotency. Reboot resumes/finalizes the original command ID.

Destructive commands use prepare/commit with a short-lived typed confirmation bound to actor, device, operation, generation, and displayed impact. Storage format reports acknowledged/unacknowledged loss, preserves enrollment/network/CA/identity/sequence/ack/OTA state, assigns a new card UUID/generation, and survives interruption. Data reset is separate from storage format, log deletion, factory configuration reset, and unclaim/revoke.

**Maintenance sleep** never implies removal of mains power. A timed sleep warns that monitoring/reporting stop. Indefinite halt requires a documented physical wake/reset path and stronger confirmation; it is not an accidental default.

No command can energize a relay/contactor, switch a load, expose a shell, or execute arbitrary scripts.
