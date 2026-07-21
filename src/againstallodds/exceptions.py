"""Project-specific exceptions with beginner-friendly messages."""


class AgainstAllOddsError(Exception):
    """Base exception for recoverable project errors."""


class ValidationError(AgainstAllOddsError):
    """Raised when a team, score, or setting is not valid."""


class DuplicateGameError(AgainstAllOddsError):
    """Raised when a game ID already exists in history."""


class StorageError(AgainstAllOddsError):
    """Raised when saved project data cannot be read or written."""
