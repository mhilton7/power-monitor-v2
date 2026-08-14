# PowerMeter V2 repository instructions

## Product invariants

- `pm-protocol/1.0.0` is the shared protocol identifier. Breaking changes require a coordinated version bump in both repositories.
- Authenticated PZEM-004T evidence is the sole source for live values, History, intervals, rollups, forecasts, completeness, energy, and usage-based cost.
- Utility-bill PDFs are rate-source documents only. Never model, persist, log, expose, or calculate from bill usage, readings, totals, balances, payments, addresses, accounts, meter identifiers, or customer identity.
- Browser code talks only to the central server. Normal firmware communication is outbound HTTPS only; firmware has no runtime web server or UI.
- Raw readings are immutable and unique by `(device_id, sequence)`. Missing values remain null and measured zero remains zero.
- Money uses exact decimal arithmetic; authoritative timestamps are UTC; SCE schedules evaluate in the configured IANA timezone.
- One-CT devices default to `energy_only`; never infer a whole-home aggregate or solar export.

## Safety and security

- Never commit secrets, certificates, credentials, database dumps, NVS dumps, build directories, or user bill documents.
- Keep TLS chain and hostname verification enabled. Keep HMAC replay protection, CSRF, session controls, permission checks, SSRF defenses, upload limits, and redaction fail-closed.
- Do not add relays, contactors, load control, remote shell, arbitrary scripting, MQTT, cloud analytics, third-party telemetry, or a sensor-side HTTP server.
- Preserve the read-only legacy repositories under `E:\Documents\Codex`; do not copy their application code.

## Change discipline

- Add tests and traceability evidence with each implementation area.
- Do not mark physical hardware certification complete without machine-readable hardware-in-loop evidence from the actual marked unit.
- Avoid placeholders, TODOs, fake production readings, disabled verification, floating image tags, or unpinned production dependencies.
