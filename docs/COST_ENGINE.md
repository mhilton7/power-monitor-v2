# Cost engine

Authoritative estimated cost has exactly two required inputs and one optional class:

```text
authenticated sensor-derived intervals
+ immutable administrator-approved rate-plan version
+ explicitly configured reusable fixed charges/credits/taxes/surcharges
= estimate
```

Bill kWh, readings, usage distribution, bill total, balance, payment, and customer identity are not inputs. A bill extraction object cannot be passed to a cost run; only the separately reviewed/published rate version can.

## Arithmetic and boundaries

Money uses Python `Decimal` and PostgreSQL `NUMERIC`; energy uses exact integer Wh/mWh where practical. Internal calculations remain unrounded. Presentation/report rules perform the only rounding.

Intervals split at local TOU period, season, effective-date, midnight, billing-cycle, and timezone/DST boundaries. Supported reusable models are flat, billing-cycle tiered, TOU, TOU with baseline credit, tiered/TOU hybrid, daily fixed charge, configured recurring tax/surcharge/credit, and CCA/Direct Access generation adjustment. Baseline credit is capped by configured allocation. Historical intervals retain the rate version used; a current rate is not silently applied retroactively.

Cost scopes are `energy_only`, `allocated_account`, and `full_account`. A one-CT device defaults to `energy_only`; the system never promotes it automatically or infers solar export.

## Outputs and disclosure

Scopes include today, yesterday, this/last week, this month, billing-cycle-to-date/projected, selected range, and current cost/hour at present authenticated load. Each result records monitored scope, rate/algorithm version, fixed-charge inclusion, baseline configuration, CCA/Direct Access configuration, completeness, missing intervals, and recalculation identity.

All estimates disclose that they may differ from a utility bill because of sensor accuracy, unmonitored loads, rate changes, taxes/credits, rounding, and utility adjustments. No actual-versus-imported-bill comparison exists.
