"""Application failures with concise messages and stable process exit codes."""


class AppError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class ConfigError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)


class CredentialError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=3)


class BackendUnavailable(CredentialError):
    """The selected secret backend cannot be accessed, not invalid credentials."""


class AuthenticationError(CredentialError):
    """HoYoLAB rejected an authenticated read."""


class BrowserImportError(CredentialError):
    """A browser session cannot be read; manual input remains available."""


class NetworkError(AppError):
    def __init__(self, message: str, *, transient: bool = True) -> None:
        super().__init__(message)
        self.transient = transient


class UnsupportedDataError(AppError):
    """The configured operation or returned data cannot be represented safely."""
