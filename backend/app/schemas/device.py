from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..constants import MAX_READING_RECORDS

StrictDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]


class ElectricalMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    measured_at: datetime | None
    monotonic_us: int = Field(ge=0)
    voltage_v: StrictDecimal | None = Field(default=None, ge=0, le=300)
    current_a: StrictDecimal | None = Field(default=None, ge=0, le=1000)
    active_power_w: StrictDecimal | None = Field(default=None, ge=0, le=300_000)
    frequency_hz: StrictDecimal | None = Field(default=None, ge=40, le=70)
    power_factor: StrictDecimal | None = Field(default=None, ge=0, le=1)
    pzem_energy_wh: int | None = Field(default=None, ge=0)
    pzem_status: Literal[
        "ok", "timeout", "bad_crc", "short_frame", "wrong_address", "invalid", "absent"
    ]
    pzem_error_code: str | None = Field(default=None, max_length=80)

    @model_validator(mode="after")
    def missing_when_meter_not_ok(self) -> ElectricalMeasurement:
        if self.measured_at is not None and self.measured_at.utcoffset() is None:
            raise ValueError("measurement timestamp must include a UTC offset")
        fields = (
            self.voltage_v,
            self.current_a,
            self.active_power_w,
            self.frequency_hz,
            self.power_factor,
        )
        if self.pzem_status != "ok" and any(value is not None for value in fields):
            raise ValueError("electrical values must be null when PZEM status is not ok")
        return self


class HeartbeatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: Literal["pm-protocol/1.0.0"]
    boot_id: str = Field(min_length=36, max_length=36)
    firmware_version: str = Field(min_length=1, max_length=80)
    measurement: ElectricalMeasurement
    storage_status: Literal["ok", "missing", "read_only", "full", "corrupt", "degraded"]
    time_status: Literal["trusted", "untrusted", "stepped", "disputed"]
    wifi_rssi: int | None = Field(default=None, ge=-127, le=0)
    ip_address: str | None = Field(default=None, max_length=45)
    backlog: int = Field(ge=0)
    oldest_sequence: int | None = Field(default=None, ge=1)
    newest_sequence: int | None = Field(default=None, ge=1)
    acknowledged_sequence: int = Field(ge=0)
    free_internal_heap: int | None = Field(default=None, ge=0)
    largest_internal_block: int | None = Field(default=None, ge=0)
    task_stack_watermarks: dict[str, int] = Field(default_factory=dict)
    reboot_reason: str | None = Field(default=None, max_length=80)
    health_flags: list[str] = Field(default_factory=list, max_length=32)
    command_results: list[CommandResult] = Field(default_factory=list, max_length=16)


class DurableReading(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(gt=0)
    reset_generation: int = Field(ge=0)
    interval_start_utc: datetime | None
    interval_end_utc: datetime | None
    monotonic_start_us: int = Field(ge=0)
    monotonic_end_us: int = Field(gt=0)
    sample_count: int = Field(ge=0, le=3600)
    expected_sample_count: int = Field(gt=0, le=3600)
    voltage_mv: int | None = Field(default=None, ge=0, le=300_000)
    current_ma: int | None = Field(default=None, ge=0, le=1_000_000)
    active_power_mw: int | None = Field(default=None, ge=0, le=300_000_000)
    frequency_mhz: int | None = Field(default=None, ge=40_000, le=70_000)
    power_factor_milli: int | None = Field(default=None, ge=0, le=1000)
    pzem_energy_wh: int | None = Field(default=None, ge=0)
    interval_energy_mwh: int | None = Field(default=None, ge=0)
    energy_selection: Literal[
        "pzem_delta", "diagnostic_power_integration", "unavailable_reset", "unavailable_invalid"
    ]
    pzem_status: str = Field(min_length=1, max_length=40)
    time_trusted: bool
    flags: list[str] = Field(default_factory=list, max_length=32)
    record_crc32: int = Field(ge=0, le=4_294_967_295)

    @model_validator(mode="after")
    def validate_time_and_energy(self) -> DurableReading:
        if self.monotonic_end_us <= self.monotonic_start_us:
            raise ValueError("monotonic interval must be ordered")
        if self.sample_count > self.expected_sample_count:
            raise ValueError("sample count cannot exceed expected sample count")
        if self.time_trusted:
            if self.interval_start_utc is None or self.interval_end_utc is None:
                raise ValueError("trusted records require UTC interval timestamps")
            if (
                self.interval_start_utc.utcoffset() is None
                or self.interval_end_utc.utcoffset() is None
            ):
                raise ValueError("trusted UTC interval timestamps must include an offset")
            if self.interval_end_utc <= self.interval_start_utc:
                raise ValueError("UTC interval must be ordered")
        elif self.interval_start_utc is not None or self.interval_end_utc is not None:
            raise ValueError("untrusted records must not fabricate UTC timestamps")
        if self.energy_selection.startswith("unavailable") and self.interval_energy_mwh is not None:
            raise ValueError("unavailable energy must be null")
        if not self.energy_selection.startswith("unavailable"):
            if self.interval_energy_mwh is None:
                raise ValueError("selected PZEM energy evidence requires interval energy")
            if self.pzem_status != "ok":
                raise ValueError("selected PZEM energy evidence requires an ok PZEM status")
        if self.energy_selection == "diagnostic_power_integration" and self.active_power_mw is None:
            raise ValueError("diagnostic PZEM power integration requires active power evidence")
        return self


class ReadingBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: Literal["pm-protocol/1.0.0"]
    records: list[DurableReading] = Field(min_length=1, max_length=MAX_READING_RECORDS)

    @model_validator(mode="after")
    def strictly_ordered_unique(self) -> ReadingBatchRequest:
        sequences = [record.sequence for record in self.records]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            raise ValueError("records must have strictly increasing unique sequences")
        return self


class PermanentLossRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    first_sequence: int = Field(gt=0)
    last_sequence: int = Field(gt=0)
    reason_code: Literal[
        "time_untrusted", "record_crc", "segment_corrupt", "operator_format", "storage_failure"
    ]
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def ordered(self) -> PermanentLossRange:
        if self.last_sequence < self.first_sequence:
            raise ValueError("loss range must be ordered")
        return self


class PermanentLossRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_id: Literal["pm-protocol/1.0.0"]
    ranges: list[PermanentLossRange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def non_overlapping(self) -> PermanentLossRequest:
        ordered = sorted(self.ranges, key=lambda item: (item.first_sequence, item.last_sequence))
        if any(
            current.first_sequence <= prior.last_sequence for prior, current in pairwise(ordered)
        ):
            raise ValueError("permanent-loss ranges must not overlap")
        return self


class CommandResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str = Field(min_length=36, max_length=36)
    state: Literal[
        "accepted",
        "running",
        "succeeded",
        "failed",
        "awaiting_reboot",
        "awaiting_heartbeat",
        "rolled_back",
    ]
    progress_percent: int = Field(ge=0, le=100)
    result_code: str = Field(min_length=1, max_length=80)
    evidence: dict[str, str | int | bool | None] = Field(default_factory=dict)


class CommandEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command_id: str
    command_type: str
    not_before: datetime
    expires_at: datetime
    attempt: int
    idempotency_key: str
    required_firmware_capability: str | None
    payload: dict[str, str | int | bool | None]


class DeviceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_id: Literal["pm-protocol/1.0.0"] = "pm-protocol/1.0.0"
    server_time: datetime
    highest_contiguous_sequence: int = Field(ge=0)
    gaps: list[tuple[int, int]]
    commands: list[CommandEnvelope]


HeartbeatRequest.model_rebuild()
