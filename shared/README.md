# Shared contracts

These files are generated from the closed server models by
`scripts/generate_contracts.py`. CI regenerates them and rejects drift.

`auth-test-vectors/hmac-sha256-v1.json` is deliberately non-secret known-answer
material used by both repositories. Production secrets must never resemble or
reuse it. Any incompatible schema or canonicalization change requires a new
protocol identifier; released contracts are not edited in place.
