from __future__ import annotations


class PowerMeterError(Exception):
    code = "PM_ERROR"
    status_code = 400

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class AuthenticationError(PowerMeterError):
    code = "AUTHENTICATION_FAILED"
    status_code = 401


class PermissionDenied(PowerMeterError):
    code = "PERMISSION_DENIED"
    status_code = 403


class IntegrityConflict(PowerMeterError):
    code = "READING_INTEGRITY_CONFLICT"
    status_code = 409


class ReplayDetected(PowerMeterError):
    code = "DEVICE_NONCE_REPLAY"
    status_code = 409


class NotFound(PowerMeterError):
    code = "NOT_FOUND"
    status_code = 404


class UnsafeSource(PowerMeterError):
    code = "RATE_SOURCE_REJECTED"
    status_code = 422


class BillRateImportError(PowerMeterError):
    code = "BILL_RATE_IMPORT_REJECTED"
    status_code = 422

    def __init__(self, detail: str, *, code: str | None = None) -> None:
        super().__init__(detail)
        if code is not None:
            self.code = code


class RateWorkflowConflict(PowerMeterError):
    code = "RATE_WORKFLOW_CONFLICT"
    status_code = 409


class RateSyncBusy(PowerMeterError):
    code = "RATE_SYNC_BUSY"
    status_code = 409


class InvalidRequest(PowerMeterError):
    code = "INVALID_REQUEST"
    status_code = 422
