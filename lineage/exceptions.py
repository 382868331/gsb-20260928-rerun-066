class LineageError(Exception):
    """Base exception for lineage operations."""


class ValidationError(LineageError):
    """Raised when an object, transformation, or relation is invalid."""


class NotFoundError(LineageError):
    """Raised when a referenced entity does not exist."""


class StateConflictError(LineageError):
    """Raised when an operation conflicts with stored state."""
