"""Domain errors that are independent of the HTTP layer. api.py maps them to responses."""


class NotFound(Exception):
    """A clinic-scoped lookup found nothing (unknown id or another clinic's row)."""

    def __init__(self, what: str = "Not found"):
        super().__init__(what)


class LeaseLost(Exception):
    """A long-running job's lease heartbeat failed; another worker owns the job now."""


class PublicError(Exception):
    """A refusal on the public API (/v1/public/*), answered with the error envelope
    {"error": {"code": code, "message": message}}. Codes and messages are fixed strings
    from this package, never request values."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message
