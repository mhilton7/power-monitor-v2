# syntax=docker/dockerfile:1.7
FROM node:26.7.0-alpine3.23@sha256:ce3cc39fe3b8b2602d3b1c4d63d301e46b48c550ecb627869853ddcdda418b63 AS build
ARG VERSION=0.1.0-rc.25
ARG REVISION=unknown
ARG BUILD_TIME=unknown
ARG FRONTEND_ASSET_ID=unknown
ENV PM_FRONTEND_VERSION="${VERSION}" \
    PM_FRONTEND_REVISION="${REVISION}" \
    PM_FRONTEND_BUILD_TIME="${BUILD_TIME}" \
    PM_FRONTEND_ASSET_ID="${FRONTEND_ASSET_ID}"
WORKDIR /build
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30.4-alpine3.24@sha256:44e36330f74d4f3a1d4e222acca9e23b401fb87811a7597024502bb759c4dd49
ARG VERSION=0.1.0-rc.25
ARG REVISION=unknown
ARG BUILD_TIME=unknown
LABEL org.opencontainers.image.title="PowerMeter V2 frontend" \
      org.opencontainers.image.description="Same-origin PowerMeter V2 browser application" \
      org.opencontainers.image.source="https://github.com/mhilton7/power-monitor-v2" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.licenses="MIT"
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /build/dist /usr/share/nginx/html
USER 101
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/healthz"]
