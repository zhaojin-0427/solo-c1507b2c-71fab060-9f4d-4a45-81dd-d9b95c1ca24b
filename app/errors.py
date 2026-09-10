"""Domain errors mapped to HTTP responses in main.py."""

from __future__ import annotations

from typing import Any, Optional


class APIError(Exception):
    def __init__(self, status_code: int, code: str, message: str,
                 details: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


class NotFoundError(APIError):
    def __init__(self, message: str):
        super().__init__(404, "not_found", message)


class ConflictError(APIError):
    def __init__(self, code: str, message: str):
        super().__init__(409, code, message)


class ValidationRejected(APIError):
    def __init__(self, code: str, message: str,
                 issues: Optional[list[dict[str, Any]]] = None):
        super().__init__(422, code, message,
                         details={"issues": issues} if issues else None)
        self.issues = issues
