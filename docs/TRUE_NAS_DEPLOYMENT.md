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

The v0.1.0-rc.27 release contract retains the one-shot `initialize` service, making
eight services total. It reuses the exact API
image digest, has no network, and receives only the three file-metadata
capabilities needed to validate and repair the UI-created host paths. Every
other service is gated on its successful exit. No long-running service sees the
secrets directory.

The release workflow publishes four multi-architecture application images:
API (also used by `migrate` and `worker` with explicit commands), frontend,
gateway, and backup. The gateway image rebuilds the exact Caddy 2.11.4 source
with an exact Go builder, locked security-fixed module floors, and only standard
modules, installs exact security-fixed runtime packages over the pinned base,
and removes its unneeded privileged-port file capability so the binary remains
executable under the deployment's zero-capability policy. The workflow captures
registry-reported digests and renders a complete immutable YAML. PostgreSQL is
also version-and-digest pinned. The release asset set includes the Caddy and
database-bootstrap configurations, complete operator guides, a tracked Windows
SMB staging helper, and the auditable image-embedded initializer source. The
normal path is SMB stage, paste the complete YAML in the TrueNAS UI, and
install; it has no shell/SSH preparation step.

See `deploy/truenas/INSTALLATION.md`, `DATASET_ACLS.md`, `SECRETS.md`,
`UPGRADE.md`, and `ROLLBACK.md` for the supported operator procedure.
