class LineageError(Exception):
    """Base error for lineage operations."""


class NotFoundError(LineageError):
    pass


class AlreadyExistsError(LineageError):
    pass


class ValidationError(LineageError):
    pass


class CycleFoundError(LineageError):
    """Raised when an operation unexpectedly encounters a graph cycle."""
