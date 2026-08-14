# First run

1. Verify `/healthz` and `/api/v1/system/health` over HTTPS with the configured CA. Do not bypass certificate errors.
2. Open the web origin. The empty database exposes only the one-time owner bootstrap flow. Create a long unique password and enroll TOTP MFA when offered.
3. Set home display name, `America/Los_Angeles` (or the utility account's actual IANA timezone), billing-cycle start, and monitored scope. A one-CT circuit remains `energy_only`.
4. Create a short-lived one-time enrollment token with `sensors.enroll`. Copy it directly into the USB provisioning flow; it is never shown again or placed in browser storage.
5. Provision the headless sensor by USB using the matching firmware release instructions. Confirm CA and hostname verification, device fingerprint, PZEM variant/CT rating, microSD self-test, and successful enrollment.
6. Confirm a signed heartbeat appears on Home. Missing values must render unavailable, while a measured zero renders zero.
7. Wait for one durable interval. Confirm it appears in History independently of the live card, then retry the same sequence and verify no duplicate is created.
8. Configure a rate. Prefer a verified official SCE source candidate. A bill can be uploaded only through **Import rates from SCE bill PDF**; review allowed unit rates/rules and publish separately. Never use bill kWh, totals, readings, identity, or payment data.
9. Confirm cost disclosure shows monitored scope, rate version, fixed/baseline/CCA configuration, completeness, missing intervals, and estimate limitations.
10. Run diagnostics, an encrypted backup, and an isolated restore test. Save the exact evidence run IDs and checksums.

The first production sensor and target TrueNAS deployment remain release-candidate status until hardware-in-loop and deployment evidence are recorded.
