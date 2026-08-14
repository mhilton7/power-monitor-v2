# SCE public-page reference snapshot

Checked in UTC on **2026-08-13** against the official [SCE Time-of-Use Rate Plans](https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans) page.

| Evidence | Value |
|---|---|
| Final URL | `https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans` |
| HTTP result | `200` |
| Response bytes | `299557` |
| Response SHA-256 | `f1e42bb9f0adac1760b88f18b962b36f681db6f22973bbb3891c5ca8b27b80af` |
| ETag | `"1786644703-gzip"` |
| Last-Modified | `2026-08-13T18:11:43Z` |
| Pinned connected peer | `45.60.77.211` (from the prevalidated public DNS set) |

The response is mutable public-page evidence. Its hash is not a tariff effective date. Release/sync processing must retain its own immutable artifact and require an official tariff date or administrator confirmation.

The current page does not explicitly state holiday treatment. The production strict parser
therefore records `HOLIDAY_RULE_MISSING` and creates no normalized candidate from this page
alone. This is review evidence, not a reason to infer that holidays follow weekend pricing.

Common components observed: summer June–September; winter October–May; base service charge `$0.79/day`; TOU-D-4-9PM and TOU-D-5-8PM baseline credit `$0.10/kWh` up to configured baseline allocation; TOU-D-PRIME no baseline credit. Listed rates combine SCE delivery and generation; CCA or Direct Access generation can differ.

| Plan/season/day | Periods and displayed price per kWh |
|---|---|
| TOU-D-4-9PM summer weekday | 00:00–16:00 off-peak `$0.34`; 16:00–21:00 on-peak `$0.58`; 21:00–24:00 off-peak `$0.34` |
| TOU-D-4-9PM summer weekend | 00:00–16:00 off-peak `$0.34`; 16:00–21:00 mid-peak `$0.46`; 21:00–24:00 off-peak `$0.34` |
| TOU-D-4-9PM winter all days | 00:00–08:00 off-peak `$0.37`; 08:00–16:00 super-off-peak `$0.33`; 16:00–21:00 mid-peak `$0.51`; 21:00–24:00 off-peak `$0.37` |
| TOU-D-5-8PM summer weekday | 00:00–17:00 off-peak `$0.34`; 17:00–20:00 on-peak `$0.74`; 20:00–24:00 off-peak `$0.34` |
| TOU-D-5-8PM summer weekend | 00:00–17:00 off-peak `$0.34`; 17:00–20:00 mid-peak `$0.54`; 20:00–24:00 off-peak `$0.34` |
| TOU-D-5-8PM winter all days | 00:00–08:00 off-peak `$0.38`; 08:00–17:00 super-off-peak `$0.32`; 17:00–20:00 mid-peak `$0.60`; 20:00–24:00 off-peak `$0.38` |
| TOU-D-PRIME summer weekday | 00:00–16:00 off-peak `$0.26`; 16:00–21:00 on-peak `$0.59`; 21:00–24:00 off-peak `$0.26` |
| TOU-D-PRIME summer weekend | 00:00–16:00 off-peak `$0.26`; 16:00–21:00 mid-peak `$0.40`; 21:00–24:00 off-peak `$0.26` |
| TOU-D-PRIME winter all days | 00:00–08:00 off-peak `$0.24`; 08:00–16:00 super-off-peak `$0.24`; 16:00–21:00 mid-peak `$0.56`; 21:00–24:00 off-peak `$0.24` |

The page also displayed after-credit examples, but the normalized model stores the reusable `$0.10/kWh` capped credit rule instead of duplicating net prices. Customer-specific baseline allocation must be configured separately and never inferred from bill usage.
