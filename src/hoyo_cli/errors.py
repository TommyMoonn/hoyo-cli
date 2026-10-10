"""Safe application failures, independent of transport and terminal libraries.

Messages and hints must be application-authored text, never upstream diagnostics.
Codes describe the cause; exit codes describe the command's failure policy.
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    CONFIG_MISSING = "config_missing"
    CONFIG_INVALID = "config_invalid"
    CREDENTIALS_MISSING = "credentials_missing"
    CREDENTIALS_INVALID = "credentials_invalid"
    CREDENTIALS_CONFLICT = "credentials_conflict"
    BACKEND_UNAVAILABLE = "backend_unavailable"
    AUTH_REJECTED = "auth_rejected"
    BROWSER_IMPORT = "browser_import"
    BROWSER_LOCKED = "browser_locked"
    BROWSER_ACCESS_DENIED = "browser_access_denied"
    BROWSER_PROFILE_MISSING = "browser_profile_missing"
    BROWSER_LOGIN_MISSING = "browser_login_missing"
    BROWSER_COOKIES_CONFLICT = "browser_cookies_conflict"
    BROWSER_DECRYPTION = "browser_decryption"
    BROWSER_UNSUPPORTED = "browser_unsupported"
    BROWSER_STORAGE = "browser_storage"
    BROWSER_TIMEOUT = "browser_timeout"
    BROWSER_UNKNOWN = "browser_unknown"
    ALREADY_CHECKED_IN = "already_checked_in"
    VERIFICATION_REQUIRED = "verification_required"
    NETWORK_TIMEOUT = "network_timeout"
    CONNECTIVITY = "connectivity"
    TLS_FAILURE = "tls_failure"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    UNSUPPORTED_DATA = "unsupported_data"
    API_REJECTED = "api_rejected"
    CANCELLED = "cancelled"
    CACHE_WRITE = "cache_write"
    UNEXPECTED = "unexpected"


class AppError(Exception):
    def __init__(
        self,
        message: str,
        exit_code: int = 1,
        *,
        code: ErrorCode = ErrorCode.UNEXPECTED,
        hint: str | None = (
            "Retry the operation. If it persists, review your account configuration."
        ),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.code = code
        self.hint = hint

    @property
    def retryable(self) -> bool:
        """Whether the request policy may automatically retry a safe read."""
        return False


class ConfigError(AppError):
    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.CONFIG_INVALID,
        hint: str | None = (
            "Review your configuration file and correct the reported setting."
        ),
    ) -> None:
        super().__init__(message, exit_code=2, code=code, hint=hint)


class CredentialError(AppError):
    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.CREDENTIALS_INVALID,
        hint: str | None = "Review the selected credential source, then run hoyo init.",
    ) -> None:
        super().__init__(message, exit_code=3, code=code, hint=hint)


class BackendUnavailable(CredentialError):
    """The selected secret backend cannot be accessed, not invalid credentials."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code=ErrorCode.BACKEND_UNAVAILABLE,
            hint="Check the selected credential backend, then run hoyo init.",
        )


class AuthenticationError(CredentialError):
    """HoYoLAB rejected an authenticated request."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code=ErrorCode.AUTH_REJECTED,
            hint="Sign in to HoYoLAB again; run hoyo init.",
        )


class AlreadyCheckedInError(AppError):
    """HoYoLAB definitively reports that today's reward was already claimed."""

    def __init__(self) -> None:
        super().__init__(
            "Already checked in.", code=ErrorCode.ALREADY_CHECKED_IN, hint=None
        )


class AmbiguousClaimError(AppError):
    """One claim was attempted, but its response cannot confirm the outcome."""

    def __init__(self, cause: AppError) -> None:
        super().__init__(
            f"{cause.message} Check-in may have completed.",
            code=cause.code,
            hint="Check today's state on HoYoLAB before trying check-in again.",
        )


class BrowserImportError(CredentialError):
    """A browser session cannot be read; manual input remains available."""

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.BROWSER_IMPORT,
        hint: str | None = (
            "Retry browser import or paste a full HoYoLAB Cookie header in hoyo init."
        ),
    ) -> None:
        super().__init__(
            message,
            code=code,
            hint=hint,
        )


class NetworkError(AppError):
    def __init__(
        self,
        message: str,
        *,
        transient: bool = True,
        code: ErrorCode = ErrorCode.CONNECTIVITY,
        hint: str | None = "Check your internet connection and retry.",
    ) -> None:
        super().__init__(message, code=code, hint=hint)
        self.transient = transient

    @property
    def retryable(self) -> bool:
        return self.transient


class UnsupportedDataError(AppError):
    """The configured operation or returned data cannot be represented safely."""

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = "Check for a hoyo-cli update, then retry.",
    ) -> None:
        super().__init__(message, code=ErrorCode.UNSUPPORTED_DATA, hint=hint)
