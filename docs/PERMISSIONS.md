# Permissions

Authorization is server-enforced for every route, background operation, home, and device. UI visibility is convenience, never the enforcement boundary.

Built-in roles are Owner, Administrator, Member, and Viewer. Owners can create custom roles by selecting explicit permissions. The last enabled Owner is protected.

| Area | Permissions |
|---|---|
| Dashboard/History | `dashboard.view`, `history.view`, `history.export` |
| Billing/rates | `billing.view`, `billing.manage`, `rates.bill_import`, `rates.view`, `rates.manage`, `rates.sync` |
| Sensors | `sensors.view`, `sensors.enroll`, `sensors.configure` |
| Commands | `sensors.command.reboot`, `.sleep`, `.storage_test`, `.storage_format`, `.ota`, `.data_reset` |
| Firmware | `firmware.view`, `firmware.manage` |
| Identity | `users.view`, `users.manage` |
| Operations | `backups.view`, `backups.manage`, `logs.view`, `system.view`, `system.manage` |

Destructive operations require both the exact permission and a typed prepare/commit confirmation. A general sensor command permission does not imply storage format, data reset, credential rotation, or unclaim. Command delivery additionally verifies that the authenticated device owns the command.

Permission, role, user status, authentication, command, rate publication, backup, and restore changes create append-only audit events with actor, target, UTC time, typed action, result, and correlation ID. Audit payloads are redacted.

Recommended default: Viewer reads dashboards/History; Member additionally operates explicitly granted non-destructive workflows; Administrator manages sensors/rates/operations but cannot defeat last-owner protection; Owner manages identity and all policy. Narrow custom roles are preferred for rate reviewers and backup operators.

The browser enrollment workflow requires both `sensors.view` (to open Sensor settings and discover authorized scopes) and `sensors.enroll` (to create a one-time token). Grant both permissions to a custom sensor-installer role; neither implies billing access.

## Multi-home authorization

Every user has one or more explicit `user_home_scopes`. `users.view` returns only identities that overlap the actor's homes, and exposes only the overlapping home identifiers. Because enablement, credentials, and role membership belong to the global identity, a `users.manage` mutation is allowed only when every target home is inside the actor's scope. The target's effective permissions must also be a subset of the actor's permissions. Role assignment and custom-role creation use the same no-escalation rule.

The authenticated, permission-independent `/api/v1/home-scopes` endpoint returns
the actor's authorized homes directly from `user_home_scopes`, even before the
first device is enrolled. Public rc.3 previously carried the same scopes inside
the `sensors.view`-protected device listing; the dedicated endpoint decouples
browser-wide home selection from sensor and billing permissions. The browser
automatically uses a scope only when exactly one exists; multiple homes require
an explicit selection and zero homes remain fail-closed. Enrollment-token
creation rechecks the submitted home against `user_home_scopes`, so the browser
never supplies authority or infers a home from an existing device.

Creating a user accepts an optional explicit `home_ids` subset. Omitting it assigns the actor's complete home scope, preserving the single-home default. The server rejects unknown or out-of-scope home IDs without revealing whether they exist. Last-owner protection is evaluated independently for every affected home while those home rows are locked, so concurrent changes cannot leave a home ownerless.
