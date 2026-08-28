# SCE public-page reference snapshot

Checked in UTC on **2026-08-28** against the official [SCE Time-of-Use Rate Plans](https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans) page.

| Evidence | Value |
|---|---|
| Final URL | `https://www.sce.com/save-money/rates-financing/residential-rate-plans/time-of-use-plans` |
| HTTP result | `200` |
| HTTP content type | `text/html` |
| Redirects | `0` |
| Response bytes | `301779` |
| Response SHA-256 | `06c529356cec7de8864df0bae3a41108de8a8a8c7f7ad9058cae569b0ea9b5ec` |
| ETag | `"1787767107-gzip"` |
| Last-Modified | `2026-08-26T17:58:27Z` |

The response is mutable public-page evidence. Its hash is not a tariff effective date. Release/sync processing must retain its own immutable artifact and require an official tariff date or administrator confirmation.

The current page embeds the three TOU schedules inside the primary
`accordion-container-bg-layout` region and links separately to the Tiered Rate Plan. The
bounded production crawl parses those primary TOU sections and follows only that exact Tiered
link. It does not crawl navigation, footer, FAQ, solar, glossary, related, or educational links.

The page does not explicitly state holiday treatment or an official tariff effective date.
The parser therefore records `holiday_treatment=unresolved`, preserves the displayed rates as
`consumer_display_rounded`, and requires authoritative tariff evidence before publication. It
does not infer a holiday rule, effective date, component precision, or calculation-grade price.

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
