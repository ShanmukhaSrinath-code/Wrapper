"""Outbound HTTP.

Import from here, not from ``httpx``. A client built by hand works and silently
drops the correlation ids, which is the failure this package exists to prevent::

    from app.core.http import request

    response = await request("GET", "https://api.example.com/things")
"""

from app.core.http.breaker import CircuitBreaker, CircuitState
from app.core.http.client import (
    close_client,
    get,
    get_breaker,
    get_client,
    post,
    request,
)

__all__ = [
    "CircuitBreaker",
    "CircuitState",
    "close_client",
    "get",
    "get_breaker",
    "get_client",
    "post",
    "request",
]
