from __future__ import annotations

import errno
def retry_transient_runtime_error(error: Exception) -> bool:
    """Retry only transient planning/routing infrastructure failures."""
    if isinstance(error, (PermissionError, ValueError, TypeError, FileNotFoundError, IsADirectoryError, NotADirectoryError)):
        return False
    message = str(error).lower()
    if isinstance(error, TimeoutError):
        return "cst" not in message
    if isinstance(error, ConnectionError):
        return True
    if isinstance(error, OSError):
        return error.errno in {
            errno.EAGAIN,
            errno.EBUSY,
            errno.ECONNABORTED,
            errno.ECONNREFUSED,
            errno.ECONNRESET,
            errno.EINTR,
            errno.ENETDOWN,
            errno.ENETUNREACH,
            errno.ETIMEDOUT,
        }
    if "cst" in message or "solver" in message:
        return False
    if any(
        term in message
        for term in (
            "temporarily unavailable",
            "service unavailable",
            "connection reset",
            "connection refused",
            "network unavailable",
            "rate limit",
            "too many requests",
            "error code: 429",
            "error code: 502",
            "error code: 503",
            "error code: 504",
        )
    ):
        return True
    return error.__class__.__name__ in {"APIConnectionError", "APITimeoutError", "ConnectError", "ReadTimeout"}
