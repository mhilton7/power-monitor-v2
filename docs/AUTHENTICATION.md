# Authentication

## Browser users

Passwords use Argon2id with the repository-pinned memory, time, and parallelism parameters. Authentication creates a server-side session; the browser receives only an opaque cookie marked `HttpOnly`, `Secure`, and `SameSite=Strict`. State-changing browser requests require the exact double-submit CSRF value as both the readable CSRF cookie and `X-CSRF-Token`; the server compares both the cookie/header pair and the stored token hash without short-circuiting secret comparisons. TOTP MFA is optional per user. TOTP evaluation uses the database UTC clock, and counters are updated under a database row lock so the same counter cannot authenticate two concurrent requests.

The first owner can be created only when no user exists. The last enabled owner cannot be disabled, demoted, or removed. Session list/revoke, password change/reset, user disable, and soft remove invalidate affected access.

### Session expiry

Every session has two independent limits:

- `PM_SESSION_ABSOLUTE_HOURS` is fixed when the session is created and never slides. It is also the browser cookie `Max-Age`.
- `PM_SESSION_IDLE_MINUTES` is measured from the server-side `last_seen_at` timestamp.

For every authenticated request the API reads the authoritative database UTC clock and performs one conditional database update: the token hash must match, the session must not be revoked, absolute expiry must be in the future, and `last_seen_at` must be newer than the idle cutoff. Only that update may advance `last_seen_at`. Consequently, an idle, absolutely expired, or concurrently revoked session cannot be revived by a stale read or by clock skew between API processes. A rejected CSRF request rolls back the attempted touch. The default absolute limit is 12 hours and the default idle limit is 30 minutes; the idle limit must not exceed the absolute limit.

### Login throttling

Login throttling is database-backed and shared by every API process. The server serializes attempts with PostgreSQL row locks over two opaque HMAC-SHA256 keys:

- a normalized-email principal key, limited by `PM_LOGIN_PRINCIPAL_MAX_FAILURES`;
- a direct-network-peer key, limited by `PM_LOGIN_SOURCE_MAX_FAILURES`.

The source row is created and locked first. If it is already locked, the server does not create a new principal row; this prevents a blocked source from growing the table with arbitrary usernames. The direct ASGI peer is used intentionally; untrusted `X-Forwarded-For` input is ignored. Configure the gateway so the API sees the intended trusted peer topology. No email address or IP address is stored in throttle state. All invalid-password, disabled/deleted-user, MFA, unknown-user, and rate-limited outcomes return the same HTTP 401 problem code and detail. Unknown and locked principals still execute a valid Argon2id verification using a fixed non-credential dummy hash. This uniform response and password work prevents a login caller from using rate-limit behavior to enumerate usernames.

Failures accumulate during `PM_LOGIN_FAILURE_WINDOW_MINUTES`. Reaching either limit sets `locked_until` for `PM_LOGIN_LOCKOUT_MINUTES`; correct credentials are also rejected while either key is locked. Requests rejected during an existing lock do not extend its deadline, which prevents an attacker from keeping a victim locked indefinitely. A successful login clears only its principal failure state, not the source-wide history. A storage or locking failure aborts the login rather than bypassing the limiter. Failed and rate-limited attempts create `USER_LOGIN_FAILED` or `USER_LOGIN_RATE_LIMITED` audit events whose target is only the opaque principal hash. Audit records and application logs never contain the submitted email, password, TOTP code, cookies, raw source address, or session token.

Exact production settings and defaults:

| Environment variable | Default | Accepted range | Meaning |
| --- | ---: | ---: | --- |
| `PM_SESSION_ABSOLUTE_HOURS` | `12` | 1–168 hours | Non-sliding session and cookie lifetime |
| `PM_SESSION_IDLE_MINUTES` | `30` | 1–1440 minutes | Maximum inactivity; cannot exceed the absolute lifetime |
| `PM_LOGIN_FAILURE_WINDOW_MINUTES` | `15` | 1–1440 minutes | Failed-login accumulation window |
| `PM_LOGIN_LOCKOUT_MINUTES` | `15` | 1–1440 minutes | Lock duration after a threshold is reached |
| `PM_LOGIN_PRINCIPAL_MAX_FAILURES` | `5` | 2–100 | Failures allowed for one normalized principal |
| `PM_LOGIN_SOURCE_MAX_FAILURES` | `50` | 2–10,000 | Failures allowed for one direct network peer; cannot be lower than the principal limit |

Changing these settings affects newly evaluated requests immediately; existing sessions keep their fixed `expires_at` absolute timestamp. Shortening the idle limit can therefore expire an existing inactive session on its next request.

## Devices

Enrollment uses a short-lived one-time token over verified TLS. The server creates a permanent device UUID and high-entropy per-device secret, stores the secret encrypted at rest, and displays only a fingerprint. Token reuse, expiry, wrong home, or revoked enrollment is rejected.

Authenticated device requests carry:

```text
X-PM-Protocol: pm-protocol/1.0.0
X-PM-Device-ID
X-PM-Timestamp
X-PM-Nonce
X-PM-Content-SHA256
X-PM-Signature
```

The signature is HMAC-SHA256 over the exact canonical method, path/query, timestamp, nonce, and lowercase body hash using directional HKDF-derived keys. The server verifies content length/hash, timestamp window, one-time nonce, credential version/revocation, canonical representation, and signature in constant time before parsing measurement content. Replay and body conflicts fail closed.

Credential rotation is an authenticated durable command with overlap bounded to the rotation transaction. Revocation is immediate. Device secrets never enter the browser or diagnostics.

## Secret files

Production receives session, field-encryption, OTA-manifest, database, and backup keys through `/run/secrets/*`. See `deploy/truenas/SECRETS.md`. Inline production secrets and insecure TLS fallback are prohibited.
