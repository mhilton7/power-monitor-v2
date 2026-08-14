# Migration from the legacy power monitor

There is no in-place schema or dataset migration from `mhilton7/power-monitor` to PowerMeter V2. Preserve the old application and `/mnt/Apps/Power/...` datasets read-only while deploying V2 into `/mnt/Apps/PowerMeterV2/...`.

This exclusion is deliberate. The legacy schema contains surfaces such as `manual_account_usage`, `utility_usage_imports`, account reconciliation/manual bill adjustments, utility bill cycle drafts, and other representations that cannot prove authenticated PZEM origin or violate V2's rate-only bill boundary. Importing those rows would make provenance ambiguous.

| Legacy material | V2 treatment |
|---|---|
| readings, intervals, rollups, costs, usage imports, manual usage, gaps/cursors | do not import; V2 History begins with newly authenticated `pm-protocol/1.0.0` sensor evidence |
| bill PDFs, OCR, bill usage/totals/readings/identity/balances/payments | do not copy or import under any circumstances |
| legacy cost results/reconciliations/adjustments | do not import; recompute only from V2 sensor intervals plus approved V2 rate versions |
| users, roles, sessions, TOTP, password hashes | recreate through V2 owner/user flows; never copy sessions/MFA secrets |
| device credentials, tokens, nonces | do not import; securely re-enroll each sensor and verify fingerprints |
| rate plans/source artifacts | use only as human reference; re-fetch official source or enter/review reusable rules and publish a new immutable V2 version |
| firmware binaries/manifests | register only a newly verified release from the independent firmware repository |
| secrets/certificates/backups/logs | do not copy; create new V2 secrets/TLS and retain old evidence under its old controls |

Cutover sequence: deploy V2 separately; create owner/rates; install compatible headless firmware without erasing measurement identity unless its release procedure requires a separately approved migration; re-enroll; verify heartbeat and first durable sequence; leave legacy read-only for the operator's retention period. Never connect both servers as authoritative acknowledgment endpoints for one device without a firmware-documented handoff.

If regulatory/audit retention requires legacy access, preserve the entire legacy environment offline/read-only. Do not expose its prohibited bill/usage fields through V2 APIs, exports, backups, or UI.
