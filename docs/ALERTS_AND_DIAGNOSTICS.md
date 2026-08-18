# Alerts, logs, and diagnostics

Typed alert rules cover sensor offline or delayed, telemetry delivery failure,
PZEM unavailable, time untrusted, TLS validation failure, repeated Wi-Fi
failure, OTA failed or rolled back, energy counter reset, unresolved
cross-cycle gap energy, rate source change/failure, backup failure, and restore
test failure. Stateless firmware has no microSD or persistent-backlog alert.

Alerts follow observation, debounce, open, acknowledge/silence/maintenance,
and resolution. Repeated evidence updates one alert instead of flooding.

Logs are structured, UTC, typed, correlated, and allowlisted. Passwords,
tokens, cookies, device/OTA/encryption keys, authorization headers, private
bill fields, original PDF bytes, and full OCR text never enter normal logs.

Diagnostics expose release/protocol/build/database identity, latest accepted
telemetry, server delivery state, PZEM health, command/OTA state, resource
watermarks, and exact evidence identifiers. Normal UI does not expose storage
capacity, backlog, cursor, missing-prefix, format, or backlog-sync controls.
Legacy fields may remain hidden temporarily while RC20 devices are staged to
RC21.

Operational views are home-scoped and permission-checked. Cross-home or
unowned evidence is omitted fail closed.
