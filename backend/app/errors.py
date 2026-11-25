"""Errors that carry a stable code and a human-readable reason.

The code and message are stored on the failed job and shown to the user, so
messages should say what is wrong with *their* data, not how the code failed.
"""

from __future__ import annotations


class JobError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class SeriesValidationError(JobError):
    """The input is not a usable single CT series. Retrying will not help."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message, retryable=False)
