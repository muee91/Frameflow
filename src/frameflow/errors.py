class TaskCancelled(Exception):
    """Cancellation acknowledged by the worker after its subprocess exits."""


class TaskError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Conflict(Exception):
    """A state/revision changed; callers must refresh before retrying."""
