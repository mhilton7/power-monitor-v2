from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator


def _clean_label(value: str, *, field: str) -> str:
    cleaned = " ".join(value.strip().split())
    if not cleaned:
        raise ValueError(f"{field} cannot be blank")
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError(f"{field} contains unsupported control characters")
    return cleaned


def _clean_optional_label(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    return _clean_label(value, field=field)


class BootstrapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=14, max_length=1024)
    home_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(default="America/Los_Angeles", min_length=1, max_length=80)

    @field_validator("display_name", "home_name")
    @classmethod
    def clean_bootstrap_labels(cls, value: str, info: object) -> str:
        field_name = getattr(info, "field_name", "name")
        return _clean_label(value, field=str(field_name).replace("_", " "))

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone is not recognized") from exc
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=1024)
    totp_code: str | None = Field(default=None, pattern=r"^\d{6}$")


class EnrollmentTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home_id: str = Field(min_length=36, max_length=36)
    friendly_name: str = Field(min_length=1, max_length=120)
    ct_rating_a: Decimal = Field(gt=0, le=1000)
    pzem_variant: Literal["pzem004t-v4-classic-candidate"]
    expires_minutes: int = Field(default=15, ge=1, le=60)


class DeviceEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enrollment_token: str = Field(min_length=32, max_length=256)
    protocol_id: Literal["pm-protocol/1.0.0"]
    firmware_version: str = Field(min_length=1, max_length=80)
    hardware_fingerprint: str = Field(min_length=8, max_length=128)


class CommandCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command_type: Literal[
        "reboot",
        "maintenance_sleep",
        "sync_now",
        "diagnostics_snapshot",
        "network_self_test",
        "meter_self_test",
        "storage_self_test",
        "format_storage_prepare",
        "format_storage_commit",
        "apply_configuration",
        "data_reset_prepare",
        "data_reset_commit",
        "data_reset_cancel",
    ]
    idempotency_key: str = Field(min_length=8, max_length=100)
    payload: dict[str, str | int | bool | None] = Field(default_factory=dict)
    prepare_command_id: str | None = Field(default=None, min_length=36, max_length=36)
    confirmation_token: str | None = Field(default=None, min_length=8, max_length=200)
    typed_confirmation: str | None = Field(default=None, min_length=1, max_length=80)

    @model_validator(mode="after")
    def commit_requires_prepare(self) -> CommandCreateRequest:
        if self.command_type.endswith("_commit") and (
            not self.prepare_command_id
            or not self.confirmation_token
            or not self.typed_confirmation
        ):
            raise ValueError(
                "commit command requires prepare command, confirmation token, and typed phrase"
            )
        if not self.command_type.endswith("_commit") and (
            self.prepare_command_id or self.confirmation_token or self.typed_confirmation
        ):
            raise ValueError("prepare evidence is valid only for a commit command")
        return self


class CredentialRotationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)
    typed_confirmation: Literal["ROTATE SENSOR CREDENTIALS"]


class CredentialRotationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=8, max_length=100)


class RatePublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    effective_start: datetime
    effective_end: datetime | None = None
    administrator_confirmed_effective_date: bool
    assign_to_utility_account_id: str | None = None

    @model_validator(mode="after")
    def dates(self) -> RatePublishRequest:
        if self.effective_start.utcoffset() is None or (
            self.effective_end is not None and self.effective_end.utcoffset() is None
        ):
            raise ValueError("effective dates must include a UTC offset")
        if not self.administrator_confirmed_effective_date:
            raise ValueError(
                "effective date must be confirmed from an official source or by an administrator"
            )
        if self.effective_end and self.effective_end <= self.effective_start:
            raise ValueError("effective date range is invalid")
        return self


class RateCandidateReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_plan_name: str = Field(min_length=1, max_length=120)
    effective_start: datetime
    effective_end: datetime | None = None
    administrator_confirmed_effective_date: Literal[True]
    administrator_confirmed_provenance: Literal[True]

    @model_validator(mode="after")
    def confirmed_dates(self) -> RateCandidateReviewRequest:
        if self.effective_start.utcoffset() is None or (
            self.effective_end is not None and self.effective_end.utcoffset() is None
        ):
            raise ValueError("effective dates must include a UTC offset")
        if self.effective_end is not None and self.effective_end <= self.effective_start:
            raise ValueError("effective date range is invalid")
        return self


class RateCandidateActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    utility_account_id: str = Field(min_length=36, max_length=36)


class ManualRatePeriodRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    season: Literal["summer", "winter", "all"]
    day_type: Literal["weekday", "weekend", "holiday", "all"]
    period_name: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    start_minute: int = Field(ge=0, lt=1440)
    end_minute: int = Field(gt=0, le=1440)
    price_per_kwh: Decimal = Field(
        gt=Decimal("0"), le=Decimal("5"), max_digits=18, decimal_places=8
    )

    @model_validator(mode="after")
    def ordered_period(self) -> ManualRatePeriodRequest:
        if self.end_minute <= self.start_minute:
            raise ValueError("rate period end must be after its start")
        return self


class ManualRateCandidateRequest(BaseModel):
    """Closed, deterministic manual fallback for administrator-sourced rate facts."""

    model_config = ConfigDict(extra="forbid")
    source_title: str = Field(min_length=3, max_length=160)
    tariff_identifier: str = Field(
        min_length=2,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._()/+-]*$",
    )
    source_url: str | None = Field(default=None, min_length=12, max_length=500)
    administrator_attests_official_source: Literal[True]
    rate_plan_name: str = Field(min_length=1, max_length=120)
    rate_class: str = Field(min_length=1, max_length=80)
    effective_start: datetime
    effective_end: datetime | None = None
    daily_fixed_charge: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("20"),
        max_digits=18,
        decimal_places=8,
    )
    monthly_fixed_charge: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("500"),
        max_digits=18,
        decimal_places=8,
    )
    baseline_credit_per_kwh: Decimal = Field(
        default=Decimal("0"),
        ge=Decimal("0"),
        le=Decimal("1"),
        max_digits=18,
        decimal_places=8,
    )
    periods: list[ManualRatePeriodRequest] = Field(min_length=1, max_length=200)

    @field_validator("source_url")
    @classmethod
    def official_https_source_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or parsed.query
            or (parsed.hostname or "").lower().rstrip(".") not in {"sce.com", "www.sce.com"}
        ):
            raise ValueError("manual provenance URL must be ordinary HTTPS on an official SCE host")
        return value

    @model_validator(mode="after")
    def exact_dates_and_complete_schedule(self) -> ManualRateCandidateRequest:
        if self.effective_start.utcoffset() is None or (
            self.effective_end is not None and self.effective_end.utcoffset() is None
        ):
            raise ValueError("effective dates must include a UTC offset")
        if self.effective_end is not None and self.effective_end <= self.effective_start:
            raise ValueError("effective date range is invalid")
        keys = [
            (period.season, period.day_type, period.start_minute, period.end_minute)
            for period in self.periods
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("manual rate periods contain duplicates")
        for season in ("summer", "winter"):
            for day_type in ("weekday", "weekend", "holiday"):
                eligible = [
                    period
                    for period in self.periods
                    if period.season in (season, "all") and period.day_type in (day_type, "all")
                ]
                if not eligible:
                    raise ValueError("manual rate periods do not cover every season and day type")
                specificity = max(
                    int(period.season == season) + int(period.day_type == day_type)
                    for period in eligible
                )
                selected = sorted(
                    (
                        period
                        for period in eligible
                        if int(period.season == season) + int(period.day_type == day_type)
                        == specificity
                    ),
                    key=lambda period: (period.start_minute, period.end_minute),
                )
                if (
                    selected[0].start_minute != 0
                    or selected[-1].end_minute != 1440
                    or any(
                        first.end_minute != second.start_minute
                        for first, second in pairwise(selected)
                    )
                ):
                    raise ValueError(
                        "manual rate periods must provide gap-free, non-overlapping full days"
                    )
        return self


class RateCorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal[
        "rate_plan_name",
        "rate_class",
        "cca_or_direct_access_indicator",
        "baseline_allocation_rule",
        "baseline_credit_rate",
        "billing_period_days",
    ]
    corrected_value: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def closed_rate_only_value(self) -> RateCorrectionRequest:
        if self.field == "rate_plan_name" and self.corrected_value.upper() not in {
            "TOU-D-4-9PM",
            "TOU-D-5-8PM",
            "TOU-D-PRIME",
            "DOMESTIC",
        }:
            raise ValueError("bill-derived plan name is outside the supported SCE allowlist")
        if self.field == "rate_class" and self.corrected_value not in {
            "residential_time_of_use",
            "residential_tiered",
        }:
            raise ValueError("bill-derived rate class is outside the supported allowlist")
        if self.field == "baseline_allocation_rule" and self.corrected_value not in {
            "credit capped by administrator-configured baseline allocation",
            "daily_allowance",
        }:
            raise ValueError("baseline rule must use the reviewed structured rule")
        if self.field == "baseline_credit_rate":
            try:
                value = Decimal(self.corrected_value)
            except Exception as exc:
                raise ValueError("baseline credit must be a decimal unit rate") from exc
            if not value.is_finite() or not Decimal("0") <= value <= Decimal("10"):
                raise ValueError("baseline credit is outside the allowed range")
        if self.field == "billing_period_days":
            try:
                days = int(self.corrected_value)
            except ValueError as exc:
                raise ValueError("billing days must be a whole number") from exc
            if str(days) != self.corrected_value.strip() or not 1 <= days <= 62:
                raise ValueError("billing days must be between 1 and 62")
        return self


class UserCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=14, max_length=1024)
    role_names: list[str] = Field(min_length=1, max_length=8)
    home_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str) -> str:
        return _clean_label(value, field="display name")

    @model_validator(mode="after")
    def valid_home_scope(self) -> UserCreateRequest:
        if self.home_ids is not None:
            if len(self.home_ids) != len(set(self.home_ids)):
                raise ValueError("home IDs must be unique")
            if any(len(home_id) != 36 for home_id in self.home_ids):
                raise ValueError("home IDs must be UUID strings")
        return self


class UserUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    enabled: bool | None = None
    role_names: list[str] | None = Field(default=None, min_length=1, max_length=8)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str | None) -> str | None:
        return _clean_optional_label(value, field="display name")


class PasswordChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=14, max_length=1024)


class SelfProfileUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    email: EmailStr | None = None
    current_password: str | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("display_name")
    @classmethod
    def clean_display_name(cls, value: str | None) -> str | None:
        return _clean_optional_label(value, field="display name")

    @model_validator(mode="after")
    def email_change_requires_password(self) -> SelfProfileUpdateRequest:
        if self.email is not None and self.current_password is None:
            raise ValueError("current password is required to change email")
        if self.display_name is None and self.email is None:
            raise ValueError("at least one profile field is required")
        return self


DashboardCard = Literal["live_power", "energy", "cost", "completeness", "alerts"]


def _default_dashboard_cards() -> list[DashboardCard]:
    return ["live_power", "energy", "cost", "completeness", "alerts"]


class UserPreferencesUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dashboard_range: Literal["today", "week", "month"] = "today"
    history_range: Literal["day", "week", "month", "billing_cycle"] = "week"
    refresh_seconds: Literal[15, 30, 60, 120, 300] = 60
    power_unit: Literal["auto", "W", "kW"] = "auto"
    energy_unit: Literal["auto", "Wh", "kWh"] = "auto"
    date_format: Literal["iso", "us"] = "us"
    time_format: Literal["12h", "24h"] = "12h"
    decimal_precision: int = Field(default=2, ge=0, le=4)
    density: Literal["comfortable", "compact"] = "comfortable"
    dashboard_cards: list[DashboardCard] = Field(
        default_factory=_default_dashboard_cards,
        min_length=1,
        max_length=5,
    )

    @field_validator("dashboard_cards")
    @classmethod
    def unique_dashboard_cards(cls, value: list[DashboardCard]) -> list[DashboardCard]:
        if len(value) != len(set(value)):
            raise ValueError("dashboard cards must be unique")
        return value


class AdminPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    new_password: str = Field(min_length=14, max_length=1024)


class MfaConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^\d{6}$")


class RoleCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z][A-Za-z0-9 _-]*$")
    description: str = Field(default="", max_length=300)
    permissions: list[str] = Field(min_length=1, max_length=64)


class HomeUtilityUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home_name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = Field(default=None, min_length=1, max_length=80)
    billing_day: int | None = Field(default=None, ge=1, le=28)
    cost_scope: Literal["energy_only", "allocated_account", "full_account"] | None = None
    baseline_allocation_kwh: Decimal | None = Field(default=None, ge=0)
    cca_provider: str | None = Field(default=None, max_length=120)
    full_account_confirmation: Literal["I UNDERSTAND FULL ACCOUNT SCOPE"] | None = None
    allocated_account_confirmation: Literal["I VERIFIED THIS ALLOCATION SCOPE"] | None = None

    @field_validator("home_name", "cca_provider")
    @classmethod
    def clean_optional_labels(cls, value: str | None, info: object) -> str | None:
        field_name = getattr(info, "field_name", "name")
        return _clean_optional_label(value, field=str(field_name).replace("_", " "))

    @model_validator(mode="after")
    def full_account_is_explicit(self) -> HomeUtilityUpdateRequest:
        if (
            self.cost_scope == "full_account"
            and self.full_account_confirmation != "I UNDERSTAND FULL ACCOUNT SCOPE"
        ):
            raise ValueError("full-account scope requires the exact typed confirmation")
        if (
            self.cost_scope == "allocated_account"
            and self.allocated_account_confirmation != "I VERIFIED THIS ALLOCATION SCOPE"
        ):
            raise ValueError("allocated-account scope requires the exact typed confirmation")
        return self

    @field_validator("timezone")
    @classmethod
    def optional_known_timezone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone is not recognized") from exc
        return value


class HomeScope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=36, max_length=36)
    name: str


class HomeScopesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home_scopes: list[HomeScope]


class DeviceUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    friendly_name: str | None = Field(default=None, min_length=1, max_length=120)
    location: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=500)
    display_order: int | None = Field(default=None, ge=0, le=10000)
    include_in_aggregate: bool | None = None
    show_on_dashboard: bool | None = None
    monitoring_enabled: bool | None = None
    measurement_scope: Literal["energy_only", "allocated_account", "full_account"] | None = None
    measurement_scope_confirmation: (
        Literal[
            "I VERIFIED THIS METER COVERS THE FULL ACCOUNT",
            "I VERIFIED THIS ALLOCATION SCOPE",
        ]
        | None
    ) = None

    @field_validator("friendly_name", "location")
    @classmethod
    def clean_optional_labels(cls, value: str | None, info: object) -> str | None:
        field_name = getattr(info, "field_name", "name")
        return _clean_optional_label(value, field=str(field_name).replace("_", " "))

    @field_validator("notes")
    @classmethod
    def clean_notes(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            return None
        if any(
            (ord(character) < 32 and character not in {"\n", "\t"}) or ord(character) == 127
            for character in cleaned
        ):
            raise ValueError("notes contain unsupported control characters")
        return cleaned

    @model_validator(mode="after")
    def account_scope_is_verified(self) -> DeviceUpdateRequest:
        expected_by_scope: dict[str, str] = {
            "full_account": "I VERIFIED THIS METER COVERS THE FULL ACCOUNT",
            "allocated_account": "I VERIFIED THIS ALLOCATION SCOPE",
        }
        expected = (
            expected_by_scope.get(self.measurement_scope)
            if self.measurement_scope is not None
            else None
        )
        if expected is not None and self.measurement_scope_confirmation != expected:
            raise ValueError("account measurement scope requires the exact verification phrase")
        if self.measurement_scope in (None, "energy_only") and self.measurement_scope_confirmation:
            raise ValueError("scope verification phrase is valid only for an account scope")
        return self


class VerifiedAggregateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home_id: str = Field(min_length=36, max_length=36)
    name: str = Field(min_length=1, max_length=120)
    device_ids: list[str] = Field(min_length=2, max_length=32)
    confirmation: Literal["I VERIFIED THESE NON-OVERLAPPING METERS"]

    @model_validator(mode="after")
    def unique_devices(self) -> VerifiedAggregateRequest:
        if len(self.device_ids) != len(set(self.device_ids)):
            raise ValueError("aggregate device IDs must be unique")
        return self


class DeviceRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["REVOKE SENSOR"]


class AlertSilenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    until: datetime

    @field_validator("until")
    @classmethod
    def aware_until(cls, value: datetime) -> datetime:
        if value.utcoffset() is None:
            raise ValueError("silence timestamp must include a UTC offset")
        return value


AlertType = Literal[
    "sensor_offline",
    "heartbeat_delayed",
    "reading_backlog",
    "pzem_unavailable",
    "microsd_missing",
    "microsd_read_only",
    "microsd_nearly_full",
    "microsd_corrupt_segment",
    "time_untrusted",
    "tls_validation_failure",
    "wifi_repeated_failure",
    "ota_failed_or_rolled_back",
    "rate_source_changed",
    "rate_sync_failed",
    "backup_failed",
    "restore_test_failed",
]


class AlertMaintenanceWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    home_id: str = Field(min_length=36, max_length=36)
    device_id: str | None = Field(default=None, min_length=36, max_length=36)
    alert_type: AlertType | None = None
    starts_at: datetime
    ends_at: datetime
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def valid_window(self) -> AlertMaintenanceWindowRequest:
        if self.starts_at.utcoffset() is None or self.ends_at.utcoffset() is None:
            raise ValueError("maintenance timestamps must include a UTC offset")
        if self.ends_at <= self.starts_at:
            raise ValueError("maintenance window end must follow start")
        if self.ends_at - self.starts_at > timedelta(days=30):
            raise ValueError("maintenance window cannot exceed 30 days")
        return self


class FirmwareDeploymentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_ids: list[str] = Field(min_length=1, max_length=100)
    rollout: Literal["immediate", "staged"] = "staged"

    @model_validator(mode="after")
    def unique_devices(self) -> FirmwareDeploymentRequest:
        if len(self.device_ids) != len(set(self.device_ids)):
            raise ValueError("firmware deployment device IDs must be unique")
        return self


class FirmwareDeploymentRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_ids: list[str] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_devices(self) -> FirmwareDeploymentRetryRequest:
        if len(self.device_ids) != len(set(self.device_ids)):
            raise ValueError("firmware retry device IDs must be unique")
        return self


class ProblemDetail(BaseModel):
    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    correlation_id: str
    errors: list[dict[str, Any]] | None = None
