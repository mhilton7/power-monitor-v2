# Device enrollment

1. An administrator with `sensors.enroll` creates a one-time enrollment token for a home, friendly name, expected CT rating, and expiration no longer than operationally necessary.
2. The administrator physically connects the ESP32-S3 over USB and runs the firmware repository's `tools/Provision-PowerMeterSensor.ps1` from its matching release.
3. Provisioning validates Wi-Fi, IPv4/DNS, server origin, trusted CA, exact hostname, PZEM protocol variant, CT rating, timezone, and token without echoing secret fields.
4. The device posts the token and hardware identity over verified HTTPS. The server atomically consumes the token, assigns a permanent UUID, stores an encrypted per-device secret, records capabilities/protocol/firmware, and emits an enrollment audit event.
5. The device commits configuration only after TLS and enrollment succeed. It retains the previous valid configuration on failure.
6. Compare the fingerprint shown by USB and server. Confirm the first signed
   stateless telemetry acceptance and exact firmware build identity.

Enrollment does not make a sensor a whole-home meter. Set circuit topology and aggregation deliberately; a single CT defaults to `energy_only`.

Revoke/unclaim is distinct from clearing readings, logs, or factory reset.
Revocation immediately rejects device authentication but never silently erases
History. RC26 firmware does not mount or modify microSD. Re-enrollment retains
the existing NVS identity/configuration unless an explicit reviewed recovery
flow says otherwise.

If provisioning fails, keep the existing identity/configuration and use USB
recovery. Temporary server unavailability alone is not a reason to erase or
replace device identity.
