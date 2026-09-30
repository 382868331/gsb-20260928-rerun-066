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


class StaleBaselineError(LineageError):
    """Raised when a draft is published over a baseline that has moved on."""

    def __init__(self, draft_id: str, base_revision: int, current_revision: int):
        self.draft_id = draft_id
        self.base_revision = base_revision
        self.current_revision = current_revision
        super().__init__(
            f"draft '{draft_id}' is based on revision {base_revision}, "
            f"but the graph is now at revision {current_revision}; "
            f"re-confirm with confirm_stale=True to publish anyway"
        )
