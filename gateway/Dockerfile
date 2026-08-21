FROM golang:1.26.6-alpine3.23@sha256:5978cc992ad5ef96a7469713c8af849c1433824761ce3be2c56381403cd8d9a3 AS builder

ENV GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org \
    GOSUMDB=sum.golang.org
WORKDIR /src

COPY gateway/go.mod gateway/go.sum ./
RUN go mod download && go mod verify

COPY gateway/main.go ./
COPY LICENSE /src/POWER-METER-V2-LICENSE.txt
RUN set -eux; \
    install -d -m 0755 /out/licenses; \
    go mod tidy -diff; \
    go list -mod=readonly -m all > /out/modules.txt; \
    grep -Fx 'github.com/caddyserver/caddy/v2 v2.11.4' /out/modules.txt; \
    grep -Fx 'golang.org/x/mod v0.40.0' /out/modules.txt; \
    grep -Fx 'golang.org/x/net v0.58.0' /out/modules.txt; \
    grep -Fx 'golang.org/x/text v0.41.0' /out/modules.txt; \
    grep -Fx 'google.golang.org/grpc v1.82.1' /out/modules.txt; \
    ldflags='-s -w -buildid= -X github.com/caddyserver/caddy/v2.CustomVersion=v2.11.4-pmv2.1'; \
    CGO_ENABLED=0 go build -mod=readonly -trimpath -buildvcs=false \
      -tags=nobadger,nomysql,nopgx \
      -ldflags "$ldflags" -o /out/caddy.first .; \
    CGO_ENABLED=0 go build -mod=readonly -trimpath -buildvcs=false \
      -tags=nobadger,nomysql,nopgx \
      -ldflags "$ldflags" -o /out/caddy.second .; \
    cmp -s /out/caddy.first /out/caddy.second; \
    mv /out/caddy.first /out/caddy; \
    rm /out/caddy.second; \
    go version -m /out/caddy > /out/caddy-buildinfo.txt; \
    grep -Fx '/out/caddy: go1.26.6' /out/caddy-buildinfo.txt; \
    grep -Eq 'dep[[:space:]]+github.com/caddyserver/caddy/v2[[:space:]]+v2\.11\.4[[:space:]]+h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=' /out/caddy-buildinfo.txt; \
    grep -Eq 'dep[[:space:]]+golang.org/x/net[[:space:]]+v0\.58\.0[[:space:]]' /out/caddy-buildinfo.txt; \
    grep -Eq 'dep[[:space:]]+golang.org/x/text[[:space:]]+v0\.41\.0[[:space:]]' /out/caddy-buildinfo.txt; \
    grep -Eq 'dep[[:space:]]+google.golang.org/grpc[[:space:]]+v1\.82\.1[[:space:]]' /out/caddy-buildinfo.txt; \
    cp /go/pkg/mod/github.com/caddyserver/caddy/v2@v2.11.4/LICENSE \
      /out/licenses/CADDY-LICENSE.txt; \
    cp /go/pkg/mod/golang.org/x/net@v0.58.0/LICENSE \
      /out/licenses/X-NET-LICENSE.txt; \
    cp /go/pkg/mod/golang.org/x/text@v0.41.0/LICENSE \
      /out/licenses/X-TEXT-LICENSE.txt; \
    cp /go/pkg/mod/google.golang.org/grpc@v1.82.1/LICENSE \
      /out/licenses/GRPC-LICENSE.txt; \
    cp /usr/local/go/LICENSE /out/licenses/GO-STDLIB-LICENSE.txt; \
    cp /src/POWER-METER-V2-LICENSE.txt \
      /out/licenses/POWER-METER-V2-LICENSE.txt

FROM caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648

ARG VERSION=0.1.0-rc.25
ARG REVISION=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="PowerMeter V2 gateway" \
      org.opencontainers.image.source="https://github.com/mhilton7/power-monitor-v2" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.base.name="docker.io/library/caddy:2.11.4-alpine" \
      org.opencontainers.image.base.digest="sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648" \
      io.powermeter.gateway.caddy.source="https://github.com/caddyserver/caddy/tree/e2eee6a7fce366321294c9c2a79f3146891dcbdf" \
      io.powermeter.gateway.caddy.module-sum="h1:XKxkMTgNSizEvKG6QHue6cAsFOteU2qA61w2tKkCWi0=" \
      io.powermeter.gateway.caddy.version="v2.11.4-pmv2.1"

COPY --from=builder /out/caddy /usr/bin/caddy
COPY --from=builder /out/caddy-buildinfo.txt /usr/share/powermeter-gateway/caddy-buildinfo.txt
COPY --from=builder /out/modules.txt /usr/share/powermeter-gateway/modules.txt
COPY --from=builder /out/licenses/ /usr/share/licenses/powermeter-gateway/

# Port 8443 does not need the inherited privileged-port file capability. Keeping
# it would make Caddy unexecutable when every process capability is dropped.
RUN set -eux; \
    /sbin/apk add --no-cache --upgrade \
      c-ares=1.34.8-r0 \
      curl=8.20.0-r0 \
      libcurl=8.20.0-r0; \
    apk info --exists 'c-ares=1.34.8-r0'; \
    apk info --exists 'curl=8.20.0-r0'; \
    apk info --exists 'libcurl=8.20.0-r0'; \
    chmod 0755 /usr/bin/caddy; \
    chmod 0644 /usr/share/powermeter-gateway/* \
      /usr/share/licenses/powermeter-gateway/*; \
    install -d -o 1000 -g 1000 -m 0750 \
      /var/log/powermeter /var/log/powermeter/gateway; \
    chown -R 1000:1000 /data /config; \
    chmod 0750 /data /config; \
    /usr/sbin/setcap -r /usr/bin/caddy 2>/dev/null || true; \
    test -z "$(/usr/sbin/getcap /usr/bin/caddy)"

USER 1000:1000
