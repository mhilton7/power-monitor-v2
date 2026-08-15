# Import rates from an SCE bill PDF

The PDF is a rate-source document only. It is not a source of consumption, History, readings, balances, identity, bill totals, calibration, forecasts, gap filling, or comparison data.

## Allowed output

The closed `RatePlanDraft` can hold only utility/plan/class, CCA or Direct Access indicator, seasons/day types/TOU boundaries, tier thresholds/units, baseline allocation rule and credit rate, per-kWh delivery/generation/component rates, recurring fixed charge, reusable recurring tax/surcharge/credit rules, explicitly printed candidate tariff dates, allowed-field page/region/confidence, parser version, and source artifact SHA-256.

It has no arbitrary JSON extension. A cost run accepts an immutable published rate version, never an extraction object.

## Prohibited data

Customer/account/address/meter identity; meter readings; hourly/daily/tier/TOU/billing-cycle usage; total kWh; demand; historical graph; billing days as a usage input; delivery/generation/tax/credit line totals; total bill; amount due; balances; payments/method/autopay/bank data; barcode identifiers; and one-time customer adjustments are discarded before output. Logs can record only `PROHIBITED_BILL_FIELD_IGNORED`, never its value.

## Workflow

1. A user with `rates.bill_import` selects **Import rates from SCE bill PDF**.
2. Server validation checks MIME plus `%PDF-` signature and the request-size limit before parser work begins. Page count, encryption, parser memory/CPU/file limits, output size, and a total deadline are enforced again inside the sandbox.
3. Development and production invoke a bundled Linux sandbox launcher. Before it reads stdin, the launcher closes every inherited descriptor except stdin/stdout/stderr, clears the environment to a fixed allowlist, disables dumps and privilege gain, applies rlimits, then installs a Landlock filesystem policy and seccomp system-call policy. The worker can read only its dedicated parser runtime/code, Tesseract data, fonts, and required system libraries. It can write only its per-job `0700` directory on the API container's `nodev,noexec,nosuid` tmpfs. `/run/secrets`, `/app`, `/data`, other `/tmp` paths, DB sockets, and all socket syscalls are inaccessible.
4. Only after the kernel boundary succeeds does the worker read the PDF from stdin. It hashes the original, prefers a usable text layer, and invokes bounded local Tesseract OCR only when required. It has no cloud OCR dependency.
5. Sensitive areas are detected/redacted and prohibited values are discarded. Temporary OCR is destroyed with the per-job tmpfs directory after success, rejection, timeout, or forced termination.
6. Stdout is the only worker-to-API channel. It is capped and validated against `pm-bill-rate-sandbox-output/1.0.0`; duplicate JSON keys, extra fields, unknown categories, invalid drafts, and a mismatched source SHA-256 fail closed. Stderr is discarded and never becomes an API or log value.
7. Only allowed fields, evidence coordinates, and confidence enter the review response. Temporary content is not stored in browser persistence.
8. The reviewer corrects allowed fields; validators enforce currency/unit, tier ordering, full non-overlapping period coverage, seasons, recurring semantics, and internal consistency.
9. An official allowlisted source is cross-checked where available. A date on a bill remains a candidate until confirmed.
10. Save/reject affects only the rate draft. Publish/assign is a separate permissioned action producing an immutable effective-dated rate version.
11. The original document bytes are released immediately after parsing and are
    never written to persistent storage, even in encrypted form. Only permitted
    rate facts, bounded evidence coordinates, parser provenance, byte/page
    counts, and the artifact SHA-256 remain. Originals cannot enter backups,
    logs, diagnostics, exports, or telemetry.

## Fail-closed production requirement

Linux Landlock ABI 3 or newer and seccomp filter mode are required. The `/tmp` mount must be a `nodev,noexec,nosuid` tmpfs. The frozen parser runtime and its entrypoint must remain root-owned and non-writable. `/health/ready` returns `503` with `pdf_sandbox: unavailable` if the kernel boundary self-test cannot prove environment clearing, outside-file denial, socket denial, private temporary storage, and sensitive-mount denial. In that state the API cannot become healthy and every non-test bill parse is refused; there is no multiprocessing or unsandboxed fallback.

The portable parser entry is named `extract_rate_plan_portable_for_tests` and raises unless `PM_ENV=test`. It exists only so cross-platform unit tests can exercise rate-only parsing; development and production never use it.

Operators can force fresh machine-readable evidence inside the API container:

```sh
python -m backend.app.bill_rate_import.sandbox_check
```

Success exits zero and emits exactly `{"pdf_sandbox":"enforced","schema_id":"pm-pdf-sandbox-health/1.0.0"}`.

Each upload is owned by exactly one authorized `home_id`; its extraction and every review transition inherit that ownership through the upload record. In the single-home default the server resolves the only authorized home. A multi-home actor must submit `home_id` explicitly. List, detail, correction, publication, assignment, and rejection queries join the extraction to its owned upload and apply the actor's home predicate before loading or locking the row. A published draft can be assigned only to a utility account in that same home.

Artifact uniqueness is `(home_id, SHA-256)`, not global SHA-256. A repeat in the same home is rejected, while an identical permitted rate-source document in a disjoint home is imported independently. This makes cross-home duplicate behavior indistinguishable from a first import and prevents an artifact-existence oracle.

## Invariants

Tests use sanitized digital, scanned, rotated, multi-page, CCA/Direct Access, invariant-rate/different-usage, different-rate/same-usage, missing-rate, line-total-only, malformed, oversized, encrypted, and OCR-timeout fixtures. Linux adversarial tests also place a sentinel secret outside the job directory, inject a sentinel environment value, and attempt a socket open; all must fail while a valid sanitized PDF still parses inside the same Landlock/seccomp boundary. They prove zero readings/intervals/rollups/History points are created; bill kWh/totals cannot affect sensor cost; prohibited values are absent from API/database/log/browser/export/diagnostics/backup; and effective-dated publication never silently rewrites prior immutable cost.

There is intentionally no historical-bills endpoint, page, comparison chart, reconciliation, usage import, or bill-payment feature.
