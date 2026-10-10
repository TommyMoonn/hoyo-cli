"""Application failures with concise messages and stable process exit codes."""


class AppError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code


class ConfigError(AppError):
    def __init__(self, message: str) -> None:
        super().__init__(message, exit_code=2)
