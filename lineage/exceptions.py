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


class BaselineChangedError(LineageError):
    """Raised when a draft was prepared against an outdated graph revision."""

    def __init__(self, message: str, *, draft_id: str, base_revision: int, current_revision: int):
        super().__init__(message)
        self.draft_id = draft_id
        self.base_revision = base_revision
        self.current_revision = current_revision
