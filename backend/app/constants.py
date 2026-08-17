from __future__ import annotations

from decimal import Decimal

PRODUCT_NAME = "PowerMeter V2"
VERSION = "0.1.0-rc.11"
PROTOCOL_ID = "pm-protocol/1.0.0"
DEFAULT_TIMEZONE = "America/Los_Angeles"
DEFAULT_HEARTBEAT_SECONDS = 15
DEFAULT_SAMPLE_SECONDS = 1
DEFAULT_INTERVAL_SECONDS = 60
MAX_READING_RECORDS = 500
MAX_READING_BODY_BYTES = 1_048_576
MAX_HEARTBEAT_BODY_BYTES = 65_536
# The ESP32 receives a JSON response into a 4096-byte C buffer. Reserve one
# byte for the terminating NUL written by the HTTP client.
MAX_DEVICE_RESPONSE_BYTES = 4_095
# CommandEnvelope.attempt is a uint8_t in pm-protocol/1.0.0 firmware.
MAX_COMMAND_DELIVERY_ATTEMPT = 255
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PDF_PAGES = 50
MAX_FIRMWARE_BYTES = 8 * 1024 * 1024
NONCE_WINDOW_SECONDS = 300
MAX_FUTURE_TIME_SECONDS = 300
MAX_CT_RATING = Decimal("1000")
