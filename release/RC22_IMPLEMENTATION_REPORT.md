# PowerMeter V2 RC22 implementation report

This report records the verified local RC22 implementation state on 2026-08-20. “Implemented” below means source plus automated evidence exists. It does not mean that production data was changed, firmware was installed on a physical sensor, a target TrueNAS host was changed, or a GitHub/GHCR release was published.

## Architecture

1. **Previous architecture found.** The repository still contains the former `app_main.c`, `pm_storage`, interval, backlog, adaptive-batch, contiguous-acknowledgement, and missing-prefix sources as unbuilt audit history. The production firmware graph was already the stateless `main/app_main_stateless.c` runtime requested here, so the user’s instruction to ignore already-present firmware behavior was followed; the active runtime was not needlessly rewritten.

2. **Firmware modules removed.** The production CMake graph selects only `app_main_stateless.c`, `runtime_startup.c`, `pm_config`, `pm_meter`, `pm_telemetry`, `pm_protocol`, `pm_network_v2`, commands, OTA, provisioning, and diagnostics. It excludes legacy `app_main.c`, `pm_storage`, `pm_measurement` interval persistence, `pm_network.c`, FATFS, SDMMC, the SD driver, journal rotation, backlog upload, missing-prefix logic, storage repair, and storage-format commands. The excluded source remains only for traceable historical regression tests.

3. **New telemetry protocol.** One reading is sent to `POST /api/v1/device/telemetry/v2` under telemetry contract `pm-telemetry/2.0.0`, while authentication, enrollment, commands, provisioning, and OTA retain `pm-protocol/1.0.0`. Transport remains outbound HTTPS with hostname/chain validation, directional HMAC, nonce replay protection, and signed responses.

4. **Exact telemetry payload.** The closed request contains required `telemetry_protocol`, `sensor_id`, `boot_id`, `sample_sequence`, `sampled_at`, `uptime_ms`, `pzem_status`, `firmware_version`, `firmware_build_id`, and `time_status`; optional/nullable measurement fields are `voltage_v`, `current_a`, `active_power_w`, `frequency_hz`, `power_factor`, `pzem_energy_wh`, and `wifi_rssi`, with bounded `command_results`. `firmware_build_id` is exactly 64 lowercase hexadecimal characters. The idempotency identity is `(sensor_id, boot_id, sample_sequence)`.

5. **Independent acceptance behavior.** Authentication and schema validation happen per sample. A new identity returns `accepted`; an exact retry returns `duplicate`; reuse of an accepted identity with different content fails closed. Sequence 11 is accepted even if sequence 10 never arrives, and an older accepted reading cannot regress current live state or firmware identity.

6. **Wi-Fi recovery behavior.** Firmware distinguishes disconnection, missing IP, DNS, and transport failures, services measurement/watchdog work while offline, and uses bounded exponential retry with jitter up to 60 seconds. It neither reboots nor resets configuration for an ordinary outage. On recovery, the newest reading becomes immediately eligible to send.

7. **Server recovery behavior.** Server/TLS/timeout/authentication failures have a retry path separate from Wi-Fi recovery. A failed reading is not persisted on the sensor; the current reading replaces an older unsent pending reading, so restoration does not wait for an old queue. The server records honest missing coverage and may recover only cumulative energy, never an invented power curve.

8. **NVS usage.** NVS remains limited to low-frequency identity, Wi-Fi/static-network configuration, server origin and CA trust, directional credentials, PZEM configuration, provisioning/recovery state, and signed OTA metadata. Telemetry samples, sample acknowledgements, History, and delivery queues are RAM/server-owned and are not written to NVS each cycle.

9. **Confirmation that SD is never mounted.** The clean digest-pinned ESP-IDF v6.0.2 ESP32-S3 build completed 856 targets, and the linked component graph contains no `pm_storage`, FATFS, SDMMC, or SD driver. The resulting application is 874,800 bytes with 89% of the OTA slot free. This is build/static evidence; marked-unit proof with a card inserted remains a physical certification gate.

10. **Confirmation that no persistent queue exists.** The active firmware keeps at most one in-flight sample and one newest pending sample in bounded RAM. The 120-day accelerated simulation processed 10,368,000 samples without a growing queue; failures create bounded diagnostics only and are not persisted to SD or NVS.

## History and service branches

11. **History aggregation design.** Each authenticated sample is immutable under `(device_id, boot_id, sample_sequence)`, updates one durable live-state row, and upserts the existing `normalized_intervals` row keyed by device, UTC bucket start, and `stateless_v2` source. A bucket stores average/minimum/maximum power, ending voltage/current, average frequency/power factor, energy delta, expected/received counts, completeness, gap state, finalization state, and last receive time. Current buckets are database-backed rather than process-only.

12. **History interval settings.** Server-managed telemetry choices are 2, 5, 10, 15, 30, or 60 seconds with a 5-second default. History bucket choices are 15, 30, 60, 300, or 900 seconds with a 60-second default. Changing either setting increments configuration state and does not require reflashing.

13. **Retention settings.** Derived History retention is 30, 90, 180, or 365 days, or indefinite (`null`), with 365 days as the default. Shortening retention requires the exact confirmation `DELETE EXPIRED SAVED HISTORY`. Cleanup touches only expired finalized stateless buckets not protected by selected cost evidence; immutable accepted samples, existing cost lineage, and sensor firmware are preserved.

14. **Cumulative-energy recovery behavior.** For a monotonic PZEM watt-hour counter, the server calculates the exact nonnegative delta. A delta after a connection gap is stored as a separate `connection_gap_recovered` energy event and may contribute to tier progression, while the power chart remains disconnected. A gap crossing a billing-cycle boundary is marked unresolved rather than assigned wholly to one cycle.

15. **Counter-reset behavior.** A lower new PZEM total creates immutable `counter_reset` evidence and an alert, establishes a new baseline, excludes the negative delta, and does not fabricate energy. Open resets lower projection confidence and prevent a falsely confirmed tier state.

16. **Main service member IDs.** The implementation stores immutable `Device.id` values, never display names, as service-branch membership. User-supplied UI evidence contained shortened sensor identifiers, but those installation-specific identifiers are intentionally omitted from this committed report. This task did not query or mutate the production database. Deployment must resolve and record the two exact active database IDs before creating or updating `Main service`; no IDs are invented in source.

17. **Whole-home aggregation behavior.** A designated `whole_home_total`/billing-source branch with `verified_sum` and explicit non-overlap confirmation sums member active power and interval energy only. Voltage, current, frequency, and power factor remain individual-sensor fields and are never blindly summed. Missing members are not treated as zero; the Dashboard labels a partial live total and History retains gaps.

18. **Future double-counting protections.** New sensors are excluded by default; membership is explicit by immutable ID and each sensor belongs to one branch. The API enforces one designated home-total/billing source, prevents duplicate membership, requires confirmed non-overlap and the required member set before switching the home total, and blocks parent/submeter combinations that would double count.

## SCE rates

19. **Official sources used.** Discovery is restricted to public `sce.com`/`www.sce.com` paths under `/save-money/` and `/regulatory/`. The verified roots are [SCE Residential Rate Plans](https://www.sce.com/save-money/rates-financing/residential-rate-plans) and [SCE Tariff Books / Rates & Pricing Choices](https://www.sce.com/regulatory/regulatory-information/tariff-books/rates-pricing-choices). No customer login, third-party price site, or authenticated account was used.

20. **Plans discovered.** Deterministic sanitized catalog fixtures prove a closed inventory of three discovered plan records with zero silent omissions; a current opt-in live smoke proved the public residential root was fetchable and yielded bounded links. A full live production crawl was intentionally not run, so this report does not claim a current all-plan count.

21. **Plans parsed.** The fixture catalog fully normalizes `TOU-D-4-9PM`, and the separate rate-only bill path normalizes the historical `DOMESTIC` seasonal-tiered schedule. Parser coverage also exercises additional known plan names/types, changed layouts, and parser-required states. It does not claim that every current live SCE tariff was fully parsed; incomplete plans remain visibly `requires_parser` and cannot be promoted.

22. **Explicit exclusions.** Every in-scope fixture link is parsed, marked `requires_parser`, or excluded with a stored reason. Catalog/list traversal pages are classified separately from plans. The SharePoint-hosted public tariff directory is explicitly excluded because its host is outside the production allowlist; it is never silently omitted. An incomplete crawl retains the last complete catalog and cannot promote its root ETag or last-known-good state.

23. **Season model.** Immutable rate versions hold explicit local-date season definitions and allow an `all` season only when the source truly has no seasonal distinction. Coverage is validated across the version’s effective range; billing selects seasons in the utility account’s configured IANA timezone.

24. **Day-type model.** Normalized periods support `all`, `weekday`, `weekend`, `holiday`, `event_day`, and `non_event_day`. Required day types must have complete coverage, and unresolved day/holiday semantics block publication rather than defaulting silently.

25. **Time-period model.** Periods use exact local start/end minutes with full 24-hour coverage and no overlap. Midnight-spanning source periods are normalized into ordered day segments; exact boundary minutes select one unambiguous period. DST evaluation uses local calendar semantics and converts authoritative timestamps from UTC only at evaluation/display boundaries.

26. **Tier model.** Tiers store exact Decimal start/end kWh, inclusive-boundary evidence, threshold basis/value, source label, and separated delivery, generation, credit, and other components. Tiered progression is account-cycle scoped and is not restarted by a UI date filter or individual-sensor selection.

27. **Holiday model.** A version records one explicit treatment: not applicable, same as weekday, same as weekend, explicit holiday schedule, or event-calendar-required. When applicable, an authoritative version-covered holiday/event calendar is mandatory; unresolved, duplicate, or out-of-range calendar evidence fails closed.

28. **Exact-versus-rounded rate handling.** Exact tariff or reviewed line-item values take precedence over rounded marketing comparisons. Money uses `Decimal`/database `NUMERIC`; delivery and generation remain separate and credits/fixed charges retain applicability so they cannot be double-counted. Rounded display occurs only after the exact calculation.

29. **Effective-date handling.** Published rate versions and their children are immutable and carry exact `[effective_start, effective_end)` evidence. Account assignments are separately effective-dated and non-overlapping; interval cost lineage records the selected immutable version. A newer version closes/supersedes the applicable range without rewriting historical calculations.

30. **Account-baseline handling.** The tier allowance is effective-dated and owned by the utility account, rate plan, and season, allowing two homes to have different daily thresholds. For the historical summer fixture, 579.0 kWh over 30 local billing days yields exactly 19.3 kWh/day. Cycle thresholds sum the applicable local-day allowance across the actual 28/30/31-day cycle and preserve DST/local-calendar behavior.

## Billing

31. **Tier 1 threshold behavior.** The cycle threshold is the exact sum of effective daily allowances. For the 30-day historical summer fixture it is 579.0 kWh, and `usage <= threshold` remains Tier 1; 28 and 31 days resolve to 540.4 and 598.3 kWh respectively.

32. **Tier 2 crossover behavior.** `tier_1_usage = min(usage, threshold)` and `tier_2_usage = max(0, usage - threshold)`. Therefore 579.0 kWh has zero Tier 2 usage, while 579.1 kWh splits into 579.0 Tier 1 plus 0.1 Tier 2.

33. **Tier 1 rate and formula.** The historical all-in Tier 1 rate is exactly `$0.30863/kWh`: `$0.17862` delivery + `$0.11761` generation + `$0.00591` wildfire fund + `$0.00619` fixed recovery + `$0.00030` state tax. The fixture result is `579 × 0.30863 = 178.69677` dollars.

34. **Tier 2 rate and formula.** The historical all-in Tier 2 rate is exactly `$0.40962/kWh`: `$0.27961` delivery plus the same exact generation and per-kWh components. The fixture result is `372 × 0.40962 = 152.37864` dollars.

35. **Fixed service-charge behavior.** The historical account charge is `$0.76900/day`, applied once per utility account per local service day, never once per sensor. Thirty days produce `$23.07000`. Monthly, meter, minimum, or other fixed charges are projected only when their exact recurrence/applicability is known; otherwise the estimate fails closed with an explicit reason.

36. **Monthly projection formula.** After at least 24 reliable whole-home hours, `projected_usage = reliable_usage / (reliable_hours / 24) × total_cycle_days`; that usage is split through the full-cycle threshold, then `total_cycle_days × daily_service_charge` is added once. Confidence is limited/moderate/high from coverage, elapsed reliable hours, unresolved gaps, and counter resets; fewer than 24 reliable hours returns `insufficient_data`.

37. **Historical $354.15 regression result.** Exact regression passed: `$178.69677 + $152.37864 + $23.07000 = $354.14541`, rounded for display to `$354.15`. Bill-derived content is restricted to reusable rate facts; the original PDF, customer identity, addresses, accounts, balances, payments, bill usage, and meter identifiers are not persisted or used as sensor History.

38. **Previous PowerMonitor behavior recreated.** Newly written React/FastAPI code presents Current Rate Plan, Current Billing Cycle, Tier Breakdown, and Cost Summary; it exposes Tier 1/Tier 2 usage and cost, active boundary, remaining Tier 1 allowance, cost to date, projected bill, and confidence in plain language. No legacy application code was copied.

## Dashboard and timezone

39. **Cause of clipped y-axis labels.** Charts used a fixed 58-pixel Y axis plus Recharts’ separate `unit` rendering and only a 2-pixel left plot margin. Wider values such as fractional kW, currency, and `kWh` exceeded that fixed allocation and were clipped at responsive widths.

40. **Chart margin or axis-width correction.** `chartAxisFormat` now formats the complete tick text, measures candidate labels with canvas (with a deterministic character-width fallback), and allocates at least 58–66 pixels plus padding. Dashboard and History left margins increased to 8 pixels, and containment tests compare every tick box with the chart/grid bounds.

41. **Correct kW/kWh handling.** Live and individual sensor power use watts below 1,000 W and kW at/above 1,000 W. Power-history values/axes/tooltips use W or kW; daily/cycle energy uses Wh or kWh. Null stays unavailable, measured zero stays zero, and missing ranges insert null boundaries with `connectNulls={false}`, eliminating the former brown filled spans.

42. **Browser timezone detection.** Display timezone resolves in this order: a valid saved user preference, `Intl.DateTimeFormat().resolvedOptions().timeZone`, the home/server IANA timezone, then UTC. The History control labels which source selected the timezone and retains UTC as an explicit technical view.

43. **PST/PDT formatting.** `Intl.DateTimeFormat` formats axes, tooltips, summaries, gap labels, and detail timestamps in the resolved IANA zone with the short zone name. `America/Los_Angeles` therefore selects PST or PDT for the actual instant rather than hard-coding an offset.

44. **Live browser-clock implementation.** One shared `useSyncExternalStore` clock publishes `Date.now()` every second and refreshes immediately on focus or tab visibility. Only the Live display range consumes the per-second end time; static presets retain their captured anchor. The Live query range advances only on the configured refresh interval or a measurement event.

45. **Confirmation that no per-second network request was added.** The shared ticker calls no API. React Query uses the pre-existing 15/30/60/120/300-second preference cadence (or a measurement event); unit and browser tests assert that second ticks advance labels without fetches or unrelated page rerenders.

## Layout

46. **Causes of overlapping panels.** Fixed minimum card heights, nonshrinking action/header content, missing `min-width: 0`, unbroken hashes/URLs, desktop row layouts retained on narrow screens, and dialogs without a bounded independent content scroller allowed long metadata and actions to collide or overflow.

47. **Shared layout components corrected.** Cards now permit grid/flex children to shrink, wrap headers/actions, remove the 480-pixel Settings card floor, stack service/firmware/catalog rows on small screens, wrap arbitrary metadata, use responsive catalog tables, constrain dialogs to the viewport, scroll dialog bodies, and keep action rows sticky/visible. Settings navigation becomes a contained horizontal control at tablet widths and the bottom navigation replaces the sidebar on mobile.

48. **Responsive sizes tested.** Automated containment ran at `320×568`, `375×667`, `390×844`, `768×1024`, `1024×768`, `1280×720`, `1366×768`, `1440×900`, `1536×864`, and `1920×1080` across Dashboard, History, Billing, and Settings.

49. **Long-text handling.** Emails, URLs, source metadata, hashes, build IDs, command results, code tokens, and rate names use `min-width: 0`, `max-width: 100%`, and `overflow-wrap: anywhere`; actions wrap beneath content instead of covering it. Firmware technical details and deployment results are collapsed by default.

50. **Visual-regression results.** Chromium Playwright completed 44/44 tests, including six maintained screenshot references, chart tick/axis containment, brush handle size, dialogs, all ten viewports, and absence of page-level horizontal overflow. No snapshot bypass or update was used.

51. **Accessibility corrections.** Visible focus remains 3 pixels, icon controls increased to 42 pixels, headings/fields/dialogs retain semantic labels, status includes text rather than color alone, chart instructions and gap meaning are textual, and mobile dialogs have accessible names/close controls. Axe WCAG 2 A/AA, 2.1 AA, and 2.2 AA checks reported zero violations on all major pages plus the mobile bill-import modal.

## Firmware lifecycle

52. **Previous meaning of Removed.** Legacy rows inferred “Removed” from an empty `image_path`, conflating missing bytes, archived display state, deployment cleanup, and deletion. That ambiguity allowed inconsistent records and made deploy eligibility difficult to reason about.

53. **New release states.** `draft`, `validating`, `available`, `current`, `archived`, `rejected`, and `deleted` are explicit constrained states. Exactly one `current` release is permitted; only `available`/`current` releases with a valid artifact and exact 64-hex build identity can deploy.

54. **New deployment states.** Per-sensor active states are `staged`, `queued`, `downloading`, `rebooting`, and `validating`; terminal states are `succeeded`, `failed`, `rolled_back`, `timed_out`, and `cancelled`. Batch results are derived as `in_progress`, `succeeded`, `partial`, `failed`, or `cancelled`, with archive/delete represented separately. Success requires a later authenticated heartbeat from the same sensor reporting the exact semantic version and ELF build ID.

55. **Archive behavior.** A noncurrent release with no active deployment can be archived without deleting its artifact or history and is hidden from the default list. Only terminal deployment batches can be archived; release and deployment archives are independent, permission-checked, audited operations.

56. **Restore behavior.** An archived release restores to `available` only if its artifact and build identity remain valid. An archived, nondeleted deployment batch can be restored to the normal history view; retry eligibility is recalculated from the current release/artifact state rather than assumed.

57. **Permanent-deletion rules.** Release deletion requires permission plus exact semantic-version, build-number, and SHA-256 confirmation, then transactionally rechecks all locks and protections. It is refused for current, rollback-pinned, sensor-reported, shared-artifact, active/pending-deployment, or active-OTA references. A deployment batch must be terminal, archived, not held for troubleshooting, and free of active OTA references before its details can be compacted.

58. **Artifact cleanup.** Upload verifies ESP32-S3 image structure, size, full artifact SHA-256, embedded semantic version/project, and embedded ELF build ID before atomic durable placement. Deletion moves the one canonical artifact to a same-volume quarantine, commits the tombstone, restores it on transaction failure, and fsyncs relevant file/directory changes before final purge.

59. **Deployment-history cleanup.** Release deletion does not erase per-sensor result rows. Deployment deletion removes optional detail/error text only after terminal archive, retains final states and exact pre/post firmware identities, and never deletes the firmware artifact. Optional retention affects only sufficiently old archived terminal deployment tombstones and respects troubleshooting holds.

60. **Audit tombstone behavior.** Permanent actions retain release ID/version/build/artifact digest, final deployment states, actor/time/correlation evidence, and an explicit `audit_tombstone` marker while clearing sensitive or unnecessary detail. Audit events use allowlisted structured fields and contain no firmware bytes, secrets, or raw logs.

61. **Existing records reconciled.** Migration `20260820_0018` maps empty-artifact legacy releases to `deleted`, selects the newest intact artifact as `current`, and pins the prior intact release for rollback. Read APIs report deployable-state/artifact/build-ID mismatches and quarantine diagnostics; reconciliation never silently deletes inconsistent rows or artifacts.

62. **Protected releases and reasons.** Protection reasons are explicit: `current_recommended_release`, `active_or_pending_deployment`, `active_ota_action_reference`, `reported_by_sensor`, `pinned_for_rollback`, `shared_artifact_reference`, and `already_deleted`. Canonical command → release → batch → deployment row-lock ordering prevents a concurrent cancel/retry/heartbeat/delete race from bypassing those checks.

## Implementation record

63. **Frontend files changed.** Production changes are in `frontend/Dockerfile`, `package.json`, `package-lock.json`, `src/api/index.ts`, `src/api/schemas.ts`, `src/layout/AppShell.tsx`, `src/lib/firmwareUpload.ts`, `src/lib/format.ts`, `src/lib/heartbeatTicker.ts`, `src/pages/HistoryPage.tsx`, `src/pages/HomePage.tsx`, `src/pages/SettingsPage.tsx`, `src/rates/RateSourceWorkflow.tsx`, new `src/rates/SceRateCatalog.tsx`, new `src/rates/queryKeys.ts`, and `src/styles.css`. Test files are recorded in item 68.

64. **Backend files changed.** Production changes are `backend/Dockerfile`, `app/config.py`, `app/constants.py`, `app/models.py`, routes `billing.py`, `dashboard.py`, `devices.py`, and `firmware.py`, schemas `api.py`, `billing.py`, and `device.py`, services `commands.py`, `cost_engine.py`, `firmware_deployments.py`, `rate_sync.py`, `rate_workflow.py`, `sce_rate_parser.py`, new `sce_catalog.py`, plus `worker/app/jobs.py`. Deployment Dockerfiles, workflows, release tools, shared contracts, and operator documentation were updated consistently.

65. **Firmware files changed.** Runtime selection remains `CMakeLists.txt` + `main/CMakeLists.txt` with one explanatory retry-scheduling comment in `components/pm_network/pm_network_v2.c`; no already-correct stateless behavior was reimplemented. RC22 identity/release/HIL truth was updated across workflow files, operator docs, release manifests/status/schema, mirrored contracts/vectors, hardware/host tests, and `tools/Build-Release.ps1`, `build_release.py`, provisioning/repair/host-runner utilities, and journal decoder. The resulting `firmware.bin` SHA-256 is `e32acbc5ae48d59ca26a07b8f78e7bd958c6401f1e1dbc14a91b752827ca8fd6`; the ELF/build ID is `a3a799f4e914ef66b64cacaa83eedb86763ae508f2519715d967ef47fbb76ab7`. Firmware source is bound to signed local commit `34ca9510c77153964aedec6a28346f1d9ca256bb`; it was not pushed, tagged, or flashed.

66. **Database migrations.** Additive revision `20260818_0017` owns stateless samples, live state, server buckets, settings, cutovers, cumulative-energy events, and billing adjustments. New additive revision `20260820_0018` adds official-catalog evidence, account-owned effective daily tier thresholds, exact firmware build IDs, release/deployment lifecycle state, retention/holds/tombstones, and constraints. Downgrades fail closed when accepted/newer evidence exists; no migration drops or rewrites accepted History.

67. **API contracts changed.** Generated OpenAPI SHA-256 is `f15e5429ca0333dbf5f1defeef01197d8a21d2bc9e684c78463f44e279b03123`; heartbeat schema SHA-256 is `0948384ec89a94452302597f8d5745683062cdf68565e5d30e19490e7af44358`; stateless request schema SHA-256 is `f439c3f0aa9a8fcb2ba6786a08ba6923813a0eb8f81b6eeb02bdf1997c21ff27`; response schema SHA-256 is `2b86f9202c6f5c583c522e6a73170906a786250a6152e2dc9c67fde173f677b7`; the canonical vector SHA-256 is `03f11e833f1f8f2b05ca0c8e83aaed0879004198d4abcb605233e67d24748828`. Firmware manifests advance to `pm-firmware-release/1.1.0` with separate numeric `build_number` and exact ELF `firmware_build_id`, while upload remains compatible with immutable 1.0.0 manifests. Server release/cross-repository manifests likewise advance to 1.1 so build number 25 cannot be confused with the 64-hex build identity; their verifier retains explicit read-only support for immutable 1.0 artifacts.

68. **Tests added.** Backend coverage was expanded in cost, provenance, OTA lifecycle/concurrency, migrations, rate candidate/source/bill parsing, catalog closure/LKG behavior, and stateless telemetry; new sanitized SCE catalog fixtures are checked in. Frontend unit/E2E coverage was expanded for formatting/axis width, live clock, Main-service aggregation, firmware upload/lifecycle, rate workflow/catalog, plain language, responsive layout, a11y, gaps, billing, and Settings. Firmware adds stateless hardware-evidence validation and extends contract/stateless host, HIL, manifest, build-ID, no-storage, fault, long-run, and release-tool tests; physical HIL remains explicitly pending.

69. **Exact commands run.** The principal recorded invocations were:

    ```powershell
    .\.venv\Scripts\python.exe -m ruff check backend worker tests scripts deploy/truenas/initialize_host.py
    .\.venv\Scripts\python.exe -m ruff format --check backend worker tests scripts deploy/truenas/initialize_host.py
    .\.venv\Scripts\python.exe -m mypy backend worker
    .\.venv\Scripts\python.exe -m mypy --platform linux deploy/truenas/initialize_host.py
    .\.venv\Scripts\python.exe -m pytest -ra
    .\.venv\Scripts\python.exe -m pytest -ra tests\test_full_audit_runner.py
    npm --prefix frontend run check
    npm --prefix frontend run test:e2e
    .\.venv\Scripts\python.exe scripts\generate_contracts.py
    .\.venv\Scripts\python.exe scripts\validate_contracts.py
    .\.venv\Scripts\python.exe scripts\validate_firmware_contract.py --server-root . --firmware-root power-monitor-sensor-headless
    .\.venv\Scripts\python.exe scripts\validate_release.py
    $env:PM_RUN_LIVE_SCE_SMOKE = '1'
    .\.venv\Scripts\python.exe -m pytest -q backend\tests\test_sce_catalog.py::test_live_public_sce_catalog_root_is_fetchable_and_discoverable
    .\tools\Run-HostTests.ps1 -PythonPath '..\.venv\Scripts\python.exe'
    python -m pip install --require-hashes -r test/host/requirements.txt
    python tools/build_release.py --build-dir build-rc22-container --output-dir release/out/0.1.0-rc.22 --version 0.1.0-rc.22 --build-number 25 --download-base https://github.com/mhilton7/power-monitor-sensor-headless/releases/download/v0.1.0-rc.22 --hardware-status release/hardware-certification-status.json --configuration release-candidate --server-tag v0.1.0-rc.22 --dependency-audit-report test-results/dependency-audit-rc22.json
    ```

    The PostgreSQL suite ran the same pytest collection against a disposable role-separated PostgreSQL 17.10 database migrated to `20260820_0018`. The clean firmware build/assembly used the repository’s immutable-digest ESP-IDF v6.0.2 container runner; profile, stack, active-graph, artifact-identity, and dependency-audit tools ran against that build. Secrets, image-push, tag, physical flash, and production-data commands were not run.

70. **Result of every command.** Ruff check and format check passed across 114 Python files; mypy passed 97 backend/worker files and the Linux initializer. The full real-PostgreSQL suite passed 378 tests with 8 expected environment skips in 120.34 seconds; API DDL, backup writes, and bootstrap login were denied as designed. A portable run produced 370 passes, 22 expected skips, and 15 setup errors caused solely by a denied default Windows temp directory; its affected audit-runner file was rerun with a validated workspace temp directory and passed 21/21, so the initial command is recorded as failed rather than hidden. Frontend check passed lint, strict typecheck, 81 unit tests, and production build; Playwright passed 44/44. Contract generation/validation, eight JSON contract checks, the cross-repository validator, 13/13 nested firmware contract tests, and release validation passed. The opt-in official-root smoke passed 1/1. Firmware host evidence passed 115 tests with 3 native-compiler discovery skips; those three C tests then passed 3/3 under MSVC 14.50, the fault matrix passed 36/36, the 10,368,000-sample simulation passed, the final ESP-IDF build/profile/active-graph/stack checks passed (322 functions, 17 objects, maximum first-party frame 2,576 ≤ 3,072 bytes), and the dependency audit reported 3 subjects/0 vulnerabilities. Local firmware release assembly passed at `power-monitor-sensor-headless/release/out/0.1.0-rc.22`: 24 files total (`SHA256SUMS` plus 23 covered artifacts), and every recorded checksum matched. Release/YAML identity tests passed 88 with 3 expected environment skips; strict build-number/build-ID separation, renderer, verifier, both Compose configurations, and hardware-certification v2 semantics passed. All four production Dockerfiles built locally for amd64 and cross-built for `linux/amd64,linux/arm64`; local validation image IDs were API `02dc8dbc…cf94a5`, frontend `e9cc8525…40bd70`, gateway `fe285613…b77db`, and backup `31771c5c…31ba8`. These local IDs are not claimed as GHCR registry-index digests. No target-TrueNAS run, remote GHCR scan/attestation, marked-unit HIL, 72-hour soak, or physical OTA was run.

71. **`git diff --name-only`.** Immediately before the signed root commit, `git diff --name-only` plus `git ls-files --others --exclude-standard` identified exactly 103 root implementation/report paths. The signed firmware commit contains exactly 42 tracked firmware paths. The firmware paths are represented by local commit `34ca9510c77153964aedec6a28346f1d9ca256bb`; the exact reviewed path inventory was:

    <details>
    <summary>Root repository tracked paths</summary>

    ```text
    .github/workflows/ci.yml
    .github/workflows/release.yml
    .github/workflows/stable-promotion.yml
    .gitleaks.toml
    backend/Dockerfile
    backend/alembic/versions/20260820_0018_rate_catalog_firmware_lifecycle.py
    backend/app/config.py
    backend/app/constants.py
    backend/app/models.py
    backend/app/routes/billing.py
    backend/app/routes/dashboard.py
    backend/app/routes/devices.py
    backend/app/routes/firmware.py
    backend/app/schemas/api.py
    backend/app/schemas/billing.py
    backend/app/schemas/device.py
    backend/app/services/commands.py
    backend/app/services/cost_engine.py
    backend/app/services/firmware_deployments.py
    backend/app/services/rate_sync.py
    backend/app/services/rate_workflow.py
    backend/app/services/sce_catalog.py
    backend/app/services/sce_rate_parser.py
    backend/tests/fixtures/sce_catalog/catalog-root.html
    backend/tests/fixtures/sce_catalog/layout-only.html
    backend/tests/fixtures/sce_catalog/schedule-d-residential.pdf
    backend/tests/fixtures/sce_catalog/time-of-use-plans-malformed.html
    backend/tests/fixtures/sce_catalog/time-of-use-plans-v1.html
    backend/tests/fixtures/sce_catalog/time-of-use-plans-v2.html
    backend/tests/fixtures/sce_catalog/tou-d-4-9.html
    backend/tests/fixtures/sce_catalog/tou-d-5-8.html
    backend/tests/test_cost_engine.py
    backend/tests/test_cost_provenance.py
    backend/tests/test_firmware_ota_contract.py
    backend/tests/test_frozen_initial_migration.py
    backend/tests/test_rate_candidate_workflow.py
    backend/tests/test_rate_source_sync.py
    backend/tests/test_sce_domestic_bill_parser.py
    backend/tests/test_sce_catalog.py
    backend/tests/test_stateless_telemetry_v2.py
    backup/Dockerfile
    deploy/truenas/INSTALLATION.md
    deploy/truenas/ROLLBACK.md
    deploy/truenas/UPGRADE.md
    docs/ALERTS_AND_DIAGNOSTICS.md
    docs/DEVICE_ENROLLMENT.md
    docs/FIRMWARE_RELEASES.md
    docs/FIRST_RUN.md
    docs/HISTORY.md
    docs/INSTALLATION.md
    docs/RELEASE_PROCESS.md
    docs/REQUIREMENTS_TRACEABILITY.md
    docs/TESTING.md
    docs/TRUE_NAS_DEPLOYMENT.md
    frontend/Dockerfile
    frontend/package-lock.json
    frontend/package.json
    frontend/src/api/index.ts
    frontend/src/api/schemas.ts
    frontend/src/layout/AppShell.tsx
    frontend/src/lib/firmwareUpload.ts
    frontend/src/lib/format.ts
    frontend/src/lib/heartbeatTicker.ts
    frontend/src/pages/HistoryPage.tsx
    frontend/src/pages/HomePage.tsx
    frontend/src/pages/SettingsPage.tsx
    frontend/src/rates/RateSourceWorkflow.tsx
    frontend/src/rates/SceRateCatalog.tsx
    frontend/src/rates/queryKeys.ts
    frontend/src/styles.css
    frontend/tests/e2e/billing-settings.spec.ts
    frontend/tests/e2e/history.spec.ts
    frontend/tests/e2e/mock-server.ts
    frontend/tests/e2e/mocks.ts
    frontend/tests/e2e/rate-workflow.spec.ts
    frontend/tests/e2e/responsive-accessibility.spec.ts
    frontend/tests/firmware-upload.test.ts
    frontend/tests/fixtures.ts
    frontend/tests/format.test.ts
    frontend/tests/heartbeat-age.test.tsx
    frontend/tests/home.test.tsx
    frontend/tests/plain-language.test.tsx
    frontend/tests/rate-workflow.test.tsx
    frontend/tests/settings.test.tsx
    gateway/Dockerfile
    pyproject.toml
    release/README.md
    release/RC22_IMPLEMENTATION_REPORT.md
    release/RELEASE_NOTES.md
    scripts/generate_contracts.py
    scripts/render_truenas_release.py
    scripts/verify_hardware_certification.py
    scripts/verify_release_artifacts.py
    shared/openapi/power-meter-v2.openapi.json
    shared/schemas/bill-rate-plan-draft.schema.json
    shared/schemas/device-heartbeat.schema.json
    shared/schemas/device-stateless-telemetry-v2.schema.json
    shared/telemetry-test-vectors/stateless-telemetry-v2.json
    tests/test_ci_release_evidence.py
    tests/test_contract_generation.py
    tests/test_dependency_lock.py
    tests/test_release_tools.py
    worker/app/jobs.py
    ```
    </details>

    <details>
    <summary>Firmware repository tracked paths</summary>

    ```text
    .github/workflows/ci.yml
    .github/workflows/release.yml
    CMakeLists.txt
    README.md
    components/pm_network/pm_network_v2.c
    docs/BUILD_AND_FLASH.md
    docs/COMMANDS.md
    docs/COM_RECOVERY.md
    docs/DATA_RESET.md
    docs/DEVICE_PROTOCOL.md
    docs/FIRST_RUN.md
    docs/HARDWARE_CERTIFICATION.md
    docs/HARDWARE_IDENTITY.md
    docs/NETWORK_RELIABILITY.md
    docs/POWERSHELL_PROVISIONING.md
    docs/PZEM_DRIVER.md
    docs/RELEASE_PROCESS.md
    docs/REQUIREMENTS_TRACEABILITY.md
    docs/STORAGE_FORMAT.md
    docs/STORAGE_RECOVERY.md
    docs/TESTING.md
    docs/WIRING.md
    release/MIGRATION.md
    release/RELEASE_NOTES.md
    release/hardware-certification-status.json
    release/hardware-certification.schema.json
    release/manifest.schema.json
    test/contracts/device-heartbeat.schema.json
    test/contracts/device-stateless-telemetry-v2.schema.json
    test/hardware/run_hil.py
    test/hardware/verify_evidence.py
    test/host/test_contract.py
    test/host/test_stateless_hardware_evidence.py
    test/host/test_stateless_telemetry.py
    test/vectors/server-contract.json
    test/vectors/stateless-telemetry-v2.json
    tools/Build-Release.ps1
    tools/Provision-PowerMeterSensor.ps1
    tools/Repair-PowerMeterSensor.ps1
    tools/Run-HostTests.ps1
    tools/build_release.py
    tools/decode_journal.py
    ```
    </details>

72. **Deployment instructions.** First independently verify and publish the assembled `power-monitor-sensor-headless/release/out/0.1.0-rc.22` package without installing it; its manifest binds firmware `0.1.0-rc.22` build 25, commit `34ca9510c77153964aedec6a28346f1d9ca256bb`, the image/build hashes in item 65, server `0.1.0-rc.22`, and pending hardware certification. Then complete all four multi-architecture image builds, publish immutable GHCR indexes, record registry digests, render the digest-pinned RC22 TrueNAS YAML/manifest from those registry digests, back up PostgreSQL, deploy server/frontend, and migrate to `20260820_0018`. Verify telemetry-v2 acceptance, current-bucket creation, and legacy-device compatibility; resolve the two immutable production sensor IDs and configure `Main service`. OTA only the first sensor, preserve NVS, do not touch its SD card, and require an authenticated post-reboot version/build-ID match plus live/History/Wi-Fi/server-recovery checks. Only then repeat for the second sensor and verify Main-service power, energy, and billing. Local amd64 images and a local rendered YAML/manifest passed, but no GHCR publication or target deployment is claimed in this report.

73. **Cutover behavior.** Server support is additive, so old firmware may continue during the transition. The first independently accepted v2 sample creates a cutover record containing the immutable device ID, old/new protocols, time, sample, firmware version, and build ID. Only after both production sensors are directly verified on v2 should operators disable active backlog processing, hide normal SD/backlog controls, and leave legacy synchronization evidence read-only; no card journal is represented as recovered History.

74. **Confirmation that accepted History was preserved.** Revisions 0017/0018 are additive, immutable PostgreSQL triggers protect accepted v2 evidence, retention excludes raw samples and selected-cost lineage, and migration/downgrade tests fail closed on evidence loss. This work did not reset or mutate a live production database, so no before/after production row-count comparison is claimed; operators must capture the required counts around the target migration.

75. **Confirmation that neither SD card was formatted.** No physical sensor command, mount, read, repair, erase, or format operation was performed. The active image contains no SD storage graph, but marked-unit “never accessed with card inserted” evidence remains pending; this report does not convert static evidence into a physical claim.

76. **Confirmation that NVS was not erased.** No sensor was flashed or factory-reset, and no NVS operation was issued. The active firmware has no `nvs_flash_erase` path and keeps the existing configuration schema. The visible RC22 identities are server/frontend `0.1.0-rc.22`, firmware `0.1.0-rc.22` build 25 at signed local commit `34ca9510c77153964aedec6a28346f1d9ca256bb`, `pm-protocol/1.0.0`, `pm-telemetry/2.0.0`, migration `20260820_0018`, the exact firmware binary/ELF hashes in item 65, and the generated contract hashes in item 67.

77. **Remaining limitations supported by evidence.** No production database/Main-service mutation, physical OTA, identity/config preservation check, marked-unit PZEM reading, inserted-card access proof, outage/power-cycle/USB recovery, OTA rollback, runtime heap/stack capture, or continuous 72-hour soak was performed. The official-root smoke is not a full live SCE catalog closure, and the allowlist explicitly excludes the external SharePoint tariff directory; therefore all-current-plan completeness remains open even though offline closure/LKG behavior is tested. No target-TrueNAS install/upgrade/rollback was run, and no GitHub tag, Release, GHCR index, remote image SBOM, attestation, or remote vulnerability scan is claimed until authorized publication completes; the local firmware package does include its own verified SBOM/provenance artifacts. Aggregate fixed-charge projection across multiple rate versions remains fail-closed when exact allocation is unresolved. These are release-candidate limitations, not hidden passes.
