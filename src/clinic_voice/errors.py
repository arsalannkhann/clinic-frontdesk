"""Domain errors that are independent of the HTTP layer. api.py maps them to responses."""


class NotFound(Exception):
    """A clinic-scoped lookup found nothing (unknown id or another clinic's row)."""

    def __init__(self, what: str = "Not found"):
        super().__init__(what)
