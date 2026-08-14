# syntax=docker/dockerfile:1.12
FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8 AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build
COPY pyproject.toml ./
COPY backend ./backend
COPY worker ./worker
RUN python -m pip install --prefix=/install -r backend/requirements.lock \
    && python -m pip install --prefix=/install --no-deps .

# Build a parser-only Python runtime. The production sandbox never receives the
# API/DB dependency closure or /app source tree in its Landlock allowlist.
RUN install -d /sandbox-runtime/usr/local/bin /sandbox-runtime/usr/local/lib/python3.13 \
    && cp /usr/local/bin/python3.13 /sandbox-runtime/usr/local/bin/python3.13 \
    && cp /usr/local/lib/libpython3.13.so.1.0 /sandbox-runtime/usr/local/lib/libpython3.13.so.1.0 \
    && cp -a /usr/local/lib/python3.13/. /sandbox-runtime/usr/local/lib/python3.13/ \
    && rm -rf /sandbox-runtime/usr/local/lib/python3.13/site-packages \
      /sandbox-runtime/usr/local/lib/python3.13/ensurepip \
      /sandbox-runtime/usr/local/lib/python3.13/idlelib \
      /sandbox-runtime/usr/local/lib/python3.13/tkinter \
      /sandbox-runtime/usr/local/lib/python3.13/turtledemo \
      /sandbox-runtime/usr/local/lib/python3.13/venv \
    && install -d /sandbox-runtime/usr/local/lib/python3.13/site-packages
RUN python -m pip install \
      --disable-pip-version-check \
      --no-cache-dir \
      --no-compile \
      --no-deps \
      --target=/sandbox-runtime/usr/local/lib/python3.13/site-packages \
      annotated-types==0.8.0 \
      packaging==26.3 \
      pillow==12.3.0 \
      pydantic==2.11.7 \
      pydantic-core==2.33.2 \
      pypdf==6.16.0 \
      pypdfium2==5.13.0 \
      pytesseract==0.3.13 \
      typing-extensions==4.16.0 \
      typing-inspection==0.4.4
RUN rm -rf /sandbox-runtime/usr/local/lib/python3.13/site-packages/pypdfium2_cli
RUN install -d \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/app/bill_rate_import \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/app/schemas \
    && cp backend/__init__.py \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/__init__.py \
    && cp backend/app/__init__.py backend/app/constants.py backend/app/errors.py \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/app/ \
    && cp backend/app/schemas/__init__.py backend/app/schemas/billing.py \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/app/schemas/ \
    && cp backend/app/bill_rate_import/__init__.py \
      backend/app/bill_rate_import/ocr.py \
      backend/app/bill_rate_import/parser.py \
      backend/app/bill_rate_import/sandbox_worker.py \
      /sandbox-runtime/usr/local/lib/python3.13/site-packages/backend/app/bill_rate_import/

FROM python:3.13.14-slim-bookworm@sha256:67a1e1f215ccda113cfc024e8639049257e88f273898f595b61476d128d387e8

ARG VERSION=0.1.0-rc.1
ARG REVISION=unknown
LABEL org.opencontainers.image.title="PowerMeter V2 API and worker" \
      org.opencontainers.image.description="Authenticated PZEM-only PowerMeter V2 services" \
      org.opencontainers.image.source="https://github.com/mhilton7/power-monitor-v2" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PATH=/usr/local/bin:/usr/bin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr=5.3.0-2 \
       tesseract-ocr-eng=1:4.1.0-2 \
       fonts-dejavu-core=2.37-6 \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o 10001 -g 10001 /app /data /data/logs /data/firmware \
       /data/rate-source-artifacts /data/bill-rate-source-artifacts

COPY --from=builder /install /usr/local
COPY --from=builder /sandbox-runtime /opt/pm-pdf-sandbox/runtime
WORKDIR /app
COPY --chown=10001:10001 backend ./backend
COPY --chown=10001:10001 worker ./worker
COPY --chown=0:0 backend/app/bill_rate_import/sandbox_entry.py /opt/pm-pdf-sandbox/entry.py

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=4 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3).read()"]
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
