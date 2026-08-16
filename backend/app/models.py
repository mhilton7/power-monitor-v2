from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


def aware_utc(value: datetime) -> datetime:
    """Normalize database timestamps; SQLite drops offsets in local test runs."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_name",
        String(80),
        ForeignKey("permissions.name", ondelete="CASCADE"),
        primary_key=True,
    ),
)

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
)

user_home_scopes = Table(
    "user_home_scopes",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("home_id", String(36), ForeignKey("homes.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


Index("uq_users_email_lower", func.lower(User.email), unique=True)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    built_in: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)


class Permission(Base):
    __tablename__ = "permissions"
    name: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str] = mapped_column(String(300), default="", nullable=False)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    client_fingerprint: Mapped[str | None] = mapped_column(String(64))


class LoginThrottle(Base):
    __tablename__ = "login_throttles"
    scope: Mapped[str] = mapped_column(String(16), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    __table_args__ = (
        CheckConstraint("scope IN ('principal','source')", name="scope"),
        CheckConstraint("failure_count >= 0", name="failure_count_nonnegative"),
    )


class MfaCredential(Base):
    __tablename__ = "mfa_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_counter: Mapped[int | None] = mapped_column(BigInteger)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    event_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str | None] = mapped_column(String(80))
    target_id: Mapped[str | None] = mapped_column(String(80))
    correlation_id: Mapped[str | None] = mapped_column(String(80), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Home(Base):
    __tablename__ = "homes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), default="America/Los_Angeles", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Circuit(Base):
    __tablename__ = "circuits"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("circuits.id", ondelete="SET NULL"))
    aggregate_mode: Mapped[str] = mapped_column(String(32), default="individual", nullable=False)
    __table_args__ = (
        CheckConstraint(
            "aggregate_mode IN ('individual','verified_sum','parent_only')",
            name="aggregate_mode",
        ),
    )


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    circuit_id: Mapped[str | None] = mapped_column(ForeignKey("circuits.id", ondelete="SET NULL"))
    friendly_name: Mapped[str] = mapped_column(String(120), nullable=False)
    location: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(String(500))
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    include_in_aggregate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    show_on_dashboard: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    protocol_id: Mapped[str] = mapped_column(
        String(40), default="pm-protocol/1.0.0", nullable=False
    )
    pzem_variant: Mapped[str] = mapped_column(String(80), nullable=False)
    ct_rating_a: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    measurement_scope: Mapped[str] = mapped_column(
        String(32), default="energy_only", nullable=False
    )
    state: Mapped[str] = mapped_column(String(40), default="enrolled", nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(80))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    contiguous_ack: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    maximum_sequence: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reset_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("ct_rating_a > 0 AND ct_rating_a <= 1000", name="ct_rating"),
        CheckConstraint(
            "measurement_scope IN ('energy_only','allocated_account','full_account')",
            name="measurement_scope",
        ),
        CheckConstraint(
            "contiguous_ack >= 0 AND maximum_sequence >= 0", name="sequence_nonnegative"
        ),
    )


class DeviceCredential(Base):
    __tablename__ = "device_credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    encrypted_secret: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    key_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default="active", nullable=False, index=True)
    rotation_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    overlap_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    prepare_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("device_commands.id", ondelete="SET NULL"), unique=True
    )
    commit_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("device_commands.id", ondelete="SET NULL"), unique=True
    )
    cancel_command_id: Mapped[str | None] = mapped_column(
        ForeignKey("device_commands.id", ondelete="SET NULL"), unique=True
    )
    initiated_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("device_id", "key_version"),
        CheckConstraint(
            "state IN ('active','pending','prepared','retiring','revoked')",
            name="state",
        ),
    )


class EnrollmentToken(Base):
    __tablename__ = "enrollment_tokens"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    friendly_name: Mapped[str] = mapped_column(String(120), nullable=False)
    ct_rating_a: Mapped[Decimal] = mapped_column(Numeric(10, 3), nullable=False)
    pzem_variant: Mapped[str] = mapped_column(String(80), nullable=False)
    issued_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_by_device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id"))


class DeviceCapability(Base):
    __tablename__ = "device_capabilities"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint("device_id", "name"),)


class DeviceNonce(Base):
    __tablename__ = "device_nonces"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    __table_args__ = (UniqueConstraint("device_id", "nonce_hash"),)


class DeviceHeartbeat(Base):
    __tablename__ = "device_heartbeats"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    boot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    measured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voltage_v: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    current_a: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    active_power_w: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    frequency_hz: Mapped[Decimal | None] = mapped_column(Numeric(8, 3))
    power_factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 4))
    pzem_status: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_status: Mapped[str] = mapped_column(String(40), nullable=False)
    storage_bytes_total: Mapped[int | None] = mapped_column(BigInteger)
    storage_bytes_free: Mapped[int | None] = mapped_column(BigInteger)
    time_status: Mapped[str] = mapped_column(String(40), nullable=False)
    wifi_rssi: Mapped[int | None] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    backlog: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    oldest_sequence: Mapped[int | None] = mapped_column(BigInteger)
    newest_sequence: Mapped[int | None] = mapped_column(BigInteger)
    free_internal_heap: Mapped[int | None] = mapped_column(BigInteger)
    largest_internal_block: Mapped[int | None] = mapped_column(BigInteger)
    reboot_reason: Mapped[str | None] = mapped_column(String(80))
    health_flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)


class DeviceEvent(Base):
    __tablename__ = "device_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int | None] = mapped_column(BigInteger)
    event_code: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeviceCommand(Base):
    __tablename__ = "device_commands"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    command_type: Mapped[str] = mapped_column(String(60), nullable=False)
    issued_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    required_firmware_capability: Mapped[str | None] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    progress_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_audit_id: Mapped[str] = mapped_column(
        ForeignKey("audit_events.id", ondelete="RESTRICT")
    )
    prepare_token_hash: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("device_id", "idempotency_key"),
        CheckConstraint("progress_percent >= 0 AND progress_percent <= 100", name="progress"),
    )


class DeviceCommandAttempt(Base):
    __tablename__ = "device_command_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    command_id: Mapped[str] = mapped_column(
        ForeignKey("device_commands.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    result_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result_code: Mapped[str | None] = mapped_column(String(80))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("command_id", "attempt"),)


class FirmwareRelease(Base):
    __tablename__ = "firmware_releases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    semantic_version: Mapped[str] = mapped_column(String(40), unique=True)
    build_number: Mapped[str] = mapped_column(String(80))
    project_name: Mapped[str] = mapped_column(String(80))
    target_chip: Mapped[str] = mapped_column(String(40))
    board_profile: Mapped[str] = mapped_column(String(80))
    minimum_boot_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    minimum_protocol: Mapped[str] = mapped_column(String(40))
    minimum_config_version: Mapped[int] = mapped_column(Integer)
    image_size: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    image_path: Mapped[str] = mapped_column(String(500))
    release_notes: Mapped[str] = mapped_column(Text)
    manifest_signature: Mapped[str] = mapped_column(String(256))
    candidate: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FirmwareDeployment(Base):
    __tablename__ = "firmware_deployments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    firmware_release_id: Mapped[str] = mapped_column(ForeignKey("firmware_releases.id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id"), index=True)
    state: Mapped[str] = mapped_column(String(32), default="queued")
    progress_percent: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RawReading(Base):
    __tablename__ = "raw_readings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reset_generation: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    interval_start_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    interval_end_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    monotonic_start_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monotonic_end_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    voltage_mv: Mapped[int | None] = mapped_column(BigInteger)
    current_ma: Mapped[int | None] = mapped_column(BigInteger)
    active_power_mw: Mapped[int | None] = mapped_column(BigInteger)
    frequency_mhz: Mapped[int | None] = mapped_column(BigInteger)
    power_factor_milli: Mapped[int | None] = mapped_column(Integer)
    pzem_energy_wh: Mapped[int | None] = mapped_column(BigInteger)
    interval_energy_mwh: Mapped[int | None] = mapped_column(BigInteger)
    energy_selection: Mapped[str] = mapped_column(String(40), nullable=False)
    pzem_status: Mapped[str] = mapped_column(String(40), nullable=False)
    time_trusted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    flags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    record_crc32: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("device_id", "sequence"),
        CheckConstraint("sequence > 0", name="sequence_positive"),
        CheckConstraint(
            "sample_count >= 0 AND expected_sample_count > 0 "
            "AND sample_count <= expected_sample_count",
            name="sample_count",
        ),
        CheckConstraint(
            "interval_energy_mwh IS NULL OR interval_energy_mwh >= 0", name="energy_nonnegative"
        ),
    )


class UnavailableSequenceRange(Base):
    __tablename__ = "unavailable_sequence_ranges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    first_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authenticated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("device_id", "first_sequence", "last_sequence"),
        CheckConstraint(
            "first_sequence > 0 AND last_sequence >= first_sequence", name="ordered_range"
        ),
    )


class NormalizedInterval(Base):
    __tablename__ = "normalized_intervals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="RESTRICT"), index=True
    )
    raw_reading_id: Mapped[str] = mapped_column(
        ForeignKey("raw_readings.id", ondelete="RESTRICT"), unique=True
    )
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    energy_mwh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    average_power_mw: Mapped[int | None] = mapped_column(BigInteger)
    completeness: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    energy_selection: Mapped[str] = mapped_column(String(40), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source_authenticated: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (
        CheckConstraint("end_utc > start_utc", name="time_order"),
        CheckConstraint("energy_mwh >= 0", name="energy_nonnegative"),
        CheckConstraint("completeness >= 0 AND completeness <= 1", name="completeness"),
        CheckConstraint("source_authenticated = true", name="authenticated_source"),
    )


class Rollup(Base):
    __tablename__ = "rollups"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    bucket: Mapped[str] = mapped_column(String(20), nullable=False)
    start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    energy_mwh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completeness: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    interval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation_run_id: Mapped[str] = mapped_column(ForeignKey("calculation_runs.id"))
    __table_args__ = (UniqueConstraint("device_id", "bucket", "start_utc"),)


class CalculationRun(Base):
    __tablename__ = "calculation_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    input_first_sequence: Mapped[int | None] = mapped_column(BigInteger)
    input_last_sequence: Mapped[int | None] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UtilityAccount(Base):
    __tablename__ = "utility_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), unique=True)
    utility_name: Mapped[str] = mapped_column(String(120), default="Southern California Edison")
    timezone: Mapped[str] = mapped_column(String(80), default="America/Los_Angeles")
    billing_day: Mapped[int] = mapped_column(Integer, default=1)
    cost_scope: Mapped[str] = mapped_column(String(32), default="energy_only")
    baseline_allocation_kwh: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    cca_provider: Mapped[str | None] = mapped_column(String(120))
    __table_args__ = (
        CheckConstraint("billing_day >= 1 AND billing_day <= 28", name="billing_day"),
        CheckConstraint(
            "cost_scope IN ('energy_only','allocated_account','full_account')", name="cost_scope"
        ),
    )


class RateSource(Base):
    __tablename__ = "rate_sources"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    https_url: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    check_interval_hours: Mapped[int] = mapped_column(Integer, default=168)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_etag: Mapped[str | None] = mapped_column(String(300))
    current_last_modified: Mapped[str | None] = mapped_column(String(200))
    __table_args__ = (
        UniqueConstraint("https_url"),
        CheckConstraint("check_interval_hours >= 1", name="positive_check_interval"),
    )


class RateSourceRevision(Base):
    __tablename__ = "rate_source_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("rate_sources.id", ondelete="CASCADE"), index=True
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    etag: Mapped[str | None] = mapped_column(String(300))
    last_modified: Mapped[str | None] = mapped_column(String(200))
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("source_id", "artifact_sha256"),)


class RateSourceArtifact(Base):
    __tablename__ = "rate_source_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("rate_source_revisions.id", ondelete="CASCADE")
    )
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    __table_args__ = (UniqueConstraint("revision_id"),)


class RateSyncRun(Base):
    __tablename__ = "rate_sync_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_id: Mapped[str] = mapped_column(ForeignKey("rate_sources.id", ondelete="CASCADE"))
    home_id: Mapped[str | None] = mapped_column(
        ForeignKey("homes.id", ondelete="CASCADE"), index=True
    )
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    event_code: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("rate_source_revisions.id"))
    correlation_id: Mapped[str] = mapped_column(
        String(80), default=new_uuid, nullable=False, index=True
    )
    requested_url: Mapped[str] = mapped_column(
        String(500), default="https://www.sce.com/", nullable=False
    )
    final_url: Mapped[str | None] = mapped_column(String(500))
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_bytes: Mapped[int | None] = mapped_column(BigInteger)
    error_code: Mapped[str | None] = mapped_column(String(100))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class RateCandidate(Base):
    __tablename__ = "rate_candidates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("rate_source_revisions.id"))
    normalized_rates: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    diff: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    validation_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="review_required")
    reviewed_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    home_id: Mapped[str | None] = mapped_column(
        ForeignKey("homes.id", ondelete="RESTRICT"), index=True
    )
    canonical_input_sha256: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("source_revision_id"),
        UniqueConstraint("home_id", "canonical_input_sha256"),
        CheckConstraint(
            "(home_id IS NULL AND canonical_input_sha256 IS NULL) OR "
            "(home_id IS NOT NULL AND canonical_input_sha256 IS NOT NULL)",
            name="manual_identity_pair",
        ),
    )


class RateCandidateReview(Base):
    """An exact-home review lifecycle for a shared immutable rate candidate."""

    __tablename__ = "rate_candidate_reviews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("rate_candidates.id", ondelete="RESTRICT"), index=True
    )
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="RESTRICT"), index=True)
    selected_plan_name: Mapped[str | None] = mapped_column(String(120))
    effective_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="reviewed")
    reviewed_by_user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    rate_plan_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="RESTRICT")
    )
    utility_account_id: Mapped[str | None] = mapped_column(
        ForeignKey("utility_accounts.id", ondelete="RESTRICT")
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("candidate_id", "home_id"),
        CheckConstraint(
            "state IN ('reviewed','published','activated','rejected')",
            name="workflow_state",
        ),
        CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="effective_range",
        ),
        CheckConstraint(
            "(state = 'reviewed' AND selected_plan_name IS NOT NULL "
            "AND effective_start IS NOT NULL AND rate_plan_version_id IS NULL "
            "AND utility_account_id IS NULL AND published_at IS NULL "
            "AND activated_at IS NULL) OR "
            "(state = 'published' AND selected_plan_name IS NOT NULL "
            "AND effective_start IS NOT NULL AND rate_plan_version_id IS NOT NULL "
            "AND utility_account_id IS NULL AND published_at IS NOT NULL "
            "AND activated_at IS NULL) OR "
            "(state = 'activated' AND selected_plan_name IS NOT NULL "
            "AND effective_start IS NOT NULL AND rate_plan_version_id IS NOT NULL "
            "AND utility_account_id IS NOT NULL AND published_at IS NOT NULL "
            "AND activated_at IS NOT NULL) OR "
            "(state = 'rejected' AND rate_plan_version_id IS NULL "
            "AND utility_account_id IS NULL AND published_at IS NULL "
            "AND activated_at IS NULL)",
            name="state_evidence",
        ),
    )


class RatePlan(Base):
    __tablename__ = "rate_plans"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    utility_name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_class: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("name", "utility_name", "rate_class"),)


class RatePlanVersion(Base):
    __tablename__ = "rate_plan_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rate_plan_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plans.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    effective_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(80), nullable=False)
    pricing_model: Mapped[str] = mapped_column(String(40), nullable=False)
    daily_fixed_charge: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    monthly_fixed_charge: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    baseline_credit_per_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    cca_adjustment_per_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    surcharge_percent: Mapped[Decimal] = mapped_column(Numeric(9, 6), default=Decimal("0"))
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(40), default="cost-v1")
    state: Mapped[str] = mapped_column(String(24), default="draft", nullable=False)
    published_by_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("rate_plan_id", "version"),)


class RatePeriod(Base):
    __tablename__ = "rate_periods"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="CASCADE"), index=True
    )
    season: Mapped[str] = mapped_column(String(30), nullable=False)
    day_type: Mapped[str] = mapped_column(String(30), nullable=False)
    period_name: Mapped[str] = mapped_column(String(40), nullable=False)
    start_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    end_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    delivery_per_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    generation_per_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 8), default=Decimal("0"))
    tier_start_kwh: Mapped[Decimal] = mapped_column(Numeric(18, 6), default=Decimal("0"))
    tier_end_kwh: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND start_minute < 1440", name="start_minute"),
        CheckConstraint("end_minute > 0 AND end_minute <= 1440", name="end_minute"),
        CheckConstraint("end_minute > start_minute", name="period_order"),
    )


class RateHoliday(Base):
    __tablename__ = "rate_holidays"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="CASCADE"), index=True
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    __table_args__ = (UniqueConstraint("rate_plan_version_id", "local_date"),)


class RateAssignment(Base):
    __tablename__ = "rate_assignments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    utility_account_id: Mapped[str] = mapped_column(
        ForeignKey("utility_accounts.id", ondelete="CASCADE"), index=True
    )
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="RESTRICT")
    )
    effective_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    effective_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    assigned_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    __table_args__ = (
        UniqueConstraint("utility_account_id", "effective_start"),
        CheckConstraint(
            "effective_end IS NULL OR effective_end > effective_start",
            name="effective_range",
        ),
    )


class BillingCycle(Base):
    __tablename__ = "billing_cycles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    utility_account_id: Mapped[str] = mapped_column(
        ForeignKey("utility_accounts.id", ondelete="CASCADE"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(30), default="configured_schedule")
    __table_args__ = (UniqueConstraint("utility_account_id", "start_date"),)


class UtilityBillRateUpload(Base):
    __tablename__ = "utility_bill_rate_uploads"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(
        ForeignKey("homes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_artifact_path: Mapped[str | None] = mapped_column(String(500))
    byte_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False)
    uploaded_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    artifact_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("home_id", "artifact_sha256", name="uq_bill_rate_upload_home_artifact"),
        CheckConstraint(
            "encrypted_artifact_path IS NULL",
            name="no_original_artifact",
        ),
    )


class UtilityBillRateExtraction(Base):
    __tablename__ = "utility_bill_rate_extractions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    upload_id: Mapped[str] = mapped_column(
        ForeignKey("utility_bill_rate_uploads.id", ondelete="CASCADE"), unique=True
    )
    utility_name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_plan_name: Mapped[str] = mapped_column(String(120), nullable=False)
    rate_class: Mapped[str] = mapped_column(String(80), nullable=False)
    plan_classification: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    holiday_treatment: Mapped[str] = mapped_column(String(40), default="unresolved", nullable=False)
    cca_or_direct_access_indicator: Mapped[str | None] = mapped_column(String(80))
    season_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    day_type_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tou_period_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    tier_threshold_definitions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reusable_price_components: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    billing_period_start: Mapped[date | None] = mapped_column(Date)
    billing_period_end: Mapped[date | None] = mapped_column(Date)
    billing_period_days: Mapped[int | None] = mapped_column(Integer)
    tier_threshold_basis: Mapped[str | None] = mapped_column(String(500))
    candidate_complete: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    baseline_allocation_rule: Mapped[str | None] = mapped_column(String(500))
    baseline_credit_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    effective_start_candidate: Mapped[date | None] = mapped_column(Date)
    effective_end_candidate: Mapped[date | None] = mapped_column(Date)
    source_evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="review_required")
    reviewer_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resulting_rate_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("rate_plan_versions.id")
    )


class UtilityBillRateCorrection(Base):
    __tablename__ = "utility_bill_rate_corrections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    extraction_id: Mapped[str] = mapped_column(
        ForeignKey("utility_bill_rate_extractions.id", ondelete="CASCADE"), index=True
    )
    allowed_field: Mapped[str] = mapped_column(String(100), nullable=False)
    prior_value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    corrected_value: Mapped[str] = mapped_column(Text, nullable=False)
    corrected_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    corrected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CostRun(Base):
    __tablename__ = "cost_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="RESTRICT")
    )
    algorithm_version: Mapped[str] = mapped_column(String(40), nullable=False)
    interval_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    interval_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cost_scope: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IntervalCost(Base):
    __tablename__ = "interval_costs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    normalized_interval_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_intervals.id", ondelete="RESTRICT"), index=True
    )
    cost_run_id: Mapped[str] = mapped_column(ForeignKey("cost_runs.id", ondelete="RESTRICT"))
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="RESTRICT")
    )
    energy_mwh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    energy_cost_microdollars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_microdollars: Mapped[int] = mapped_column(BigInteger, default=0)
    period_name: Mapped[str] = mapped_column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint("normalized_interval_id", "cost_run_id"),)


class IntervalCostSelection(Base):
    __tablename__ = "interval_cost_selections"
    normalized_interval_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_intervals.id", ondelete="CASCADE"), primary_key=True
    )
    interval_cost_id: Mapped[str] = mapped_column(
        ForeignKey("interval_costs.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    selection_reason: Mapped[str] = mapped_column(
        String(80), default="effective_rate_assignment", nullable=False
    )


class BillingEstimate(Base):
    __tablename__ = "billing_estimates"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    utility_account_id: Mapped[str] = mapped_column(
        ForeignKey("utility_accounts.id", ondelete="CASCADE")
    )
    cost_run_id: Mapped[str] = mapped_column(ForeignKey("cost_runs.id", ondelete="RESTRICT"))
    rate_plan_version_id: Mapped[str] = mapped_column(
        ForeignKey("rate_plan_versions.id", ondelete="RESTRICT"), index=True
    )
    estimate_kind: Mapped[str] = mapped_column(
        String(40), default="billing_cycle_to_date", nullable=False
    )
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(80), nullable=False)
    member_device_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    scope_start_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scope_end_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sensor_energy_mwh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    energy_cost_microdollars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fixed_charge_microdollars: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    credit_microdollars: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_microdollars: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completeness: Mapped[Decimal] = mapped_column(Numeric(7, 6), nullable=False)
    missing_intervals: Mapped[int] = mapped_column(Integer, nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    calculated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class BillingEstimateSelection(Base):
    __tablename__ = "billing_estimate_selections"
    utility_account_id: Mapped[str] = mapped_column(
        ForeignKey("utility_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    estimate_kind: Mapped[str] = mapped_column(String(40), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    scope_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    billing_estimate_id: Mapped[str] = mapped_column(
        ForeignKey("billing_estimates.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="open")
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    alert_id: Mapped[str] = mapped_column(ForeignKey("alerts.id", ondelete="CASCADE"), index=True)
    event_code: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AlertConditionState(Base):
    __tablename__ = "alert_condition_states"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    scope_key: Mapped[str] = mapped_column(String(180), unique=True, nullable=False)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    observation_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_observation_key: Mapped[str | None] = mapped_column(String(180))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AlertMaintenanceWindow(Base):
    __tablename__ = "alert_maintenance_windows"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    home_id: Mapped[str] = mapped_column(ForeignKey("homes.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str | None] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    alert_type: Mapped[str | None] = mapped_column(String(80))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(300), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT")
    )
    __table_args__ = (CheckConstraint("ends_at > starts_at", name="ordered_window"),)


class NotificationSetting(Base):
    __tablename__ = "notification_settings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    alert_type: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    minimum_severity: Mapped[str] = mapped_column(String(16), default="warning")
    __table_args__ = (UniqueConstraint("user_id", "alert_type"),)


class BackupRun(Base):
    __tablename__ = "backup_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    encrypted_path: Mapped[str | None] = mapped_column(String(500))
    sha256: Mapped[str | None] = mapped_column(String(64))
    manifest_path: Mapped[str | None] = mapped_column(String(500))
    byte_count: Mapped[int | None] = mapped_column(BigInteger)
    verification_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RestoreTest(Base):
    __tablename__ = "restore_tests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    backup_run_id: Mapped[str] = mapped_column(ForeignKey("backup_runs.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    isolated_database_version: Mapped[str | None] = mapped_column(String(80))
    migration_revision: Mapped[str | None] = mapped_column(String(80))
    row_count_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ApplicationLog(Base):
    __tablename__ = "application_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    event_code: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    home_id: Mapped[str | None] = mapped_column(
        ForeignKey("homes.id", ondelete="CASCADE"), index=True
    )
    correlation_id: Mapped[str | None] = mapped_column(String(80), index=True)
    device_id: Mapped[str | None] = mapped_column(String(36), index=True)
    command_id: Mapped[str | None] = mapped_column(String(36), index=True)
    sync_id: Mapped[str | None] = mapped_column(String(36), index=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


Index("ix_raw_readings_device_time", RawReading.device_id, RawReading.interval_start_utc)
Index(
    "ix_normalized_intervals_device_time",
    NormalizedInterval.device_id,
    NormalizedInterval.start_utc,
)


def _immutable(_mapper: object, _connection: object, target: object) -> None:
    raise ValueError(f"{type(target).__name__} records are immutable")


for _model in (
    RawReading,
    UnavailableSequenceRange,
    AuditEvent,
    IntervalCost,
    BillingEstimate,
    RateSourceRevision,
    RateSourceArtifact,
):
    event.listen(_model, "before_update", _immutable)
    event.listen(_model, "before_delete", _immutable)


@event.listens_for(RateCandidate, "before_update")
def _rate_candidate_evidence_immutable(
    _mapper: object, _connection: object, target: RateCandidate
) -> None:
    from sqlalchemy import inspect

    state = inspect(target)
    immutable_fields = (
        "source_revision_id",
        "normalized_rates",
        "diff",
        "validation_evidence",
        "home_id",
        "canonical_input_sha256",
    )
    if any(state.attrs[field].history.has_changes() for field in immutable_fields):
        raise ValueError(
            "rate-candidate source, normalized values, diff, and validation are immutable"
        )


@event.listens_for(RateCandidate, "before_delete")
def _rate_candidate_not_deletable(
    _mapper: object, _connection: object, _target: RateCandidate
) -> None:
    raise ValueError("rate-candidate provenance cannot be deleted")


@event.listens_for(RateCandidateReview, "before_update")
def _published_rate_candidate_review_immutable(
    _mapper: object, _connection: object, target: RateCandidateReview
) -> None:
    from sqlalchemy import inspect

    state = inspect(target)
    state_history = state.attrs.state.history
    old_state = state_history.deleted[0] if state_history.deleted else target.state
    changed = {
        field
        for field in (
            "candidate_id",
            "home_id",
            "selected_plan_name",
            "effective_start",
            "effective_end",
            "state",
            "reviewed_by_user_id",
            "reviewed_at",
            "rate_plan_version_id",
            "utility_account_id",
            "published_at",
            "activated_at",
        )
        if state.attrs[field].history.has_changes()
    }
    immutable_identity = {
        "candidate_id",
        "home_id",
    }
    if changed & immutable_identity:
        raise ValueError("rate-candidate review identity is immutable")
    new_state = target.state
    if old_state == "reviewed" and new_state == "reviewed":
        if changed - {
            "selected_plan_name",
            "effective_start",
            "effective_end",
            "reviewed_by_user_id",
            "reviewed_at",
        }:
            raise ValueError("reviewed rate-candidate fields changed illegally")
        return
    legal_transition_fields = {
        ("reviewed", "published"): {"state", "rate_plan_version_id", "published_at"},
        ("published", "activated"): {"state", "utility_account_id", "activated_at"},
        ("reviewed", "rejected"): {"state"},
    }
    allowed = legal_transition_fields.get((old_state, new_state))
    if allowed is None or changed - allowed:
        raise ValueError("rate-candidate review lifecycle transition is illegal")


@event.listens_for(RateCandidateReview, "before_delete")
def _rate_candidate_review_not_deletable(
    _mapper: object, _connection: object, _target: RateCandidateReview
) -> None:
    raise ValueError("rate-candidate review provenance cannot be deleted")


@event.listens_for(RatePlanVersion, "before_update")
def _published_rate_immutable(
    _mapper: object, _connection: object, target: RatePlanVersion
) -> None:
    from sqlalchemy import inspect

    state_history = inspect(target).attrs.state.history
    old_state = state_history.deleted[0] if state_history.deleted else target.state
    if old_state == "published":
        raise ValueError("published rate-plan versions are immutable")


@event.listens_for(RatePlanVersion, "before_delete")
def _used_rate_not_deletable(_mapper: object, _connection: object, target: RatePlanVersion) -> None:
    if target.state == "published":
        raise ValueError("published rate-plan versions cannot be deleted")


def _published_rate_child_immutable(
    _mapper: object, connection: Connection, target: RatePeriod | RateHoliday
) -> None:
    state = connection.scalar(
        select(RatePlanVersion.state).where(RatePlanVersion.id == target.rate_plan_version_id)
    )
    if state == "published":
        raise ValueError("children of published rate-plan versions are immutable")


for _rate_child in (RatePeriod, RateHoliday):
    event.listen(_rate_child, "before_insert", _published_rate_child_immutable)
    event.listen(_rate_child, "before_update", _published_rate_child_immutable)
    event.listen(_rate_child, "before_delete", _published_rate_child_immutable)
