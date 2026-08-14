# TrueNAS deployment architecture

```text
LAN/VPN browser and sensors
             │ HTTPS :8443 only
             ▼
         Caddy gateway ───── frontend (static UI)
             │ same-origin /api, /device, /events
             ▼
            API ───────────── worker
             │                  │
             └──── internal PostgreSQL network ─── backup
```

PostgreSQL is not published. `migrate`, `api`, `worker`, and `backup` reach it through the internal `database` bridge. API alone joins the outbound `egress` bridge for strictly allowlisted SCE synchronization; worker also joins it for scheduled official-source work. Frontend is reachable only from Caddy. No service mounts the Docker socket, uses privileged mode, or receives extra Linux capabilities.

The release workflow publishes three application images: API (also used by `migrate` and `worker` with explicit commands), frontend, and backup. It captures registry-reported digests and renders a full immutable YAML. PostgreSQL and Caddy are also version-and-digest pinned.

See `deploy/truenas/INSTALLATION.md`, `DATASET_ACLS.md`, `SECRETS.md`, `UPGRADE.md`, and `ROLLBACK.md` for exact operator procedures.
