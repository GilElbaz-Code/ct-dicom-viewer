"""Errors the services raise. main.py turns them into HTTP responses; the worker stores them on the failed job."""


class NotFound(Exception):
    """The series or slice does not exist (HTTP 404)."""


class Conflict(Exception):
    """The client asked for a version of the series that was replaced by a newer one (HTTP 409)."""


class InvalidInput(Exception):
    """The request or the DICOM data cannot be used; the message tells the user why (HTTP 422, or a failed job)."""


class LeaseLost(Exception):
    """This attempt no longer has permission to publish changes."""
