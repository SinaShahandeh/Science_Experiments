"""API errors: {"error": "<code>", "message": "...", "t": ..., <fields that explain them>}."""
import time

# code -> HTTP status of the response (the body is what the CLI prints; the CLI ignores the status)
HTTP_STATUS = {
    "invalid_argument": 400, "unknown_label": 400, "temperature_limit": 400,
    "not_homed": 409, "already_loaded": 409, "bed_hot": 409, "halted": 409, "camera_moved": 409,
    "operator_declined": 409, "operator_timeout": 409,
    "move_failed": 500, "homing_failed": 500,
    "printer_unavailable": 503, "camera_unavailable": 503,
}


class ApiError(Exception):
    def __init__(self, code, message, **fields):
        super().__init__(f"{code}: {message}")
        self.code, self.message, self.fields = code, message, fields

    def body(self):
        return {"error": self.code, "message": self.message, **self.fields, "t": round(time.time(), 3)}

    @property
    def http_status(self):
        return HTTP_STATUS.get(self.code, 500)


class MoveFailed(ApiError):
    """A move couldn't be confirmed; the position counts as unknown until prepare() re-homes."""

    def __init__(self, message, **fields):
        super().__init__("move_failed", message, **fields)


class HomingFailed(ApiError):
    def __init__(self, message, **fields):
        super().__init__("homing_failed", message, **fields)
