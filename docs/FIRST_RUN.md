# First run

1. Verify `/healthz`, `/health/live`, `/health/ready`, and
   `/api/v1/auth/bootstrap/status` over HTTPS with the configured CA. Never
   bypass certificate errors.
2. Complete the one-time owner bootstrap, enroll MFA, and confirm authenticated
   System health reports server/frontend build identity and database revision.
3. Set the home timezone and billing-cycle start. One-CT devices remain
   `energy_only` unless an operator explicitly verifies a non-overlapping
   service branch.
4. Create a short-lived enrollment token and provision by USB using the exact
   matching firmware release. Preserve NVS; confirm CA/hostname verification,
   fingerprint, PZEM profile, and enrollment.
5. Confirm independently accepted stateless telemetry appears, measured zero
   stays zero, missing values stay unavailable, and a duplicate retry creates
   no second sample.
6. Create or verify `Main service` only after confirming its member sensors are
   non-overlapping. New sensors are never added automatically.
7. Confirm Home and History use Main service, connection gaps remain visible,
   and voltage/current/frequency/power-factor remain per-sensor.
8. Configure a reviewed rate. A bill may provide rate facts only; never use or
   store its usage, total, identity, address, account, payment, or meter data.
9. Confirm Billing shows the exact rate version, cycle dates, tier threshold,
   current tier, fixed charge once, missing-reading confidence, and Main
   service scope.
10. Verify encrypted backup and isolated restore evidence in Settings.

RC19 firmware does not mount or modify microSD. Tests do not flash or OTA a
physical sensor. Move sensors only in explicit operator maintenance windows,
one at a time, after the RC19 server is healthy.
