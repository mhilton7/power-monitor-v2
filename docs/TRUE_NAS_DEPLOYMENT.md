# TrueNAS deployment architecture

```text
LAN/VPN browsers and sensors
             | HTTPS :8443 only
             v
         Caddy gateway -------- frontend (static UI)
             | same-origin /api, /device, /events
             v
            API -------------- worker
             |                  |
             +--- internal PostgreSQL network --- backup
```

PostgreSQL is not published. `migrate`, `api`, `worker`, and `backup` reach it
through the internal `database` bridge. API and worker also join the `egress`
bridge for strictly allowlisted official SCE synchronization. Frontend is
reachable only from Caddy. No service mounts the Docker socket, uses privileged
mode, or receives extra Linux capabilities.

The release workflow publishes three multi-architecture application images:
API (also used by `migrate` and `worker` with explicit commands), frontend, and
backup. It captures registry-reported digests and renders a complete immutable
YAML. PostgreSQL and Caddy are also version-and-digest pinned. The release asset
set includes the Caddy and database-bootstrap configurations, complete operator
guides, and a checksum-verified TrueNAS host preparation script.

See `deploy/truenas/INSTALLATION.md`, `DATASET_ACLS.md`, `SECRETS.md`,
`UPGRADE.md`, and `ROLLBACK.md` for the supported operator procedure.
