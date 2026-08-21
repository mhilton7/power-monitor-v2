FROM postgres:17.10-alpine3.23@sha256:8189a1f6e40904781fc9e2612687877791d21679866db58b1de996b31fc312e4

ARG VERSION=0.1.0-rc.25
ARG REVISION=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="PowerMeter V2 backup" \
      org.opencontainers.image.description="Encrypted PostgreSQL backup and isolated restore verifier" \
      org.opencontainers.image.source="https://github.com/mhilton7/power-monitor-v2" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.licenses="MIT"

RUN apk add --no-cache \
      bash=5.3.3-r1 \
      coreutils=9.8-r1 \
      findutils=4.10.0-r0 \
      gnupg=2.4.9-r0 \
      jq=1.8.1-r0 \
      tzdata=2026c-r0 \
    && rm -f /usr/local/bin/gosu \
    && test ! -e /usr/local/bin/gosu \
    && addgroup -g 568 powermeter \
    && adduser -D -H -u 568 -G powermeter -s /sbin/nologin powermeter \
    && install -d -o 568 -g 568 -m 0750 \
       /opt/powermeter /backups /backups/archives /backups/status

COPY --chown=568:568 backup/common.sh backup/entrypoint.sh backup/backup.sh backup/restore.sh backup/healthcheck.sh /opt/powermeter/

RUN chmod 0555 /opt/powermeter/*.sh

USER 568:568
WORKDIR /backups
ENTRYPOINT ["/opt/powermeter/entrypoint.sh"]
