"""Auth-only rate limiting, separate from the frozen /v1 error catalogue."""

from __future__ import annotations

import math

from control.api_v1.errors import V1ApiError


class AuthRateLimit(V1ApiError):
    def __init__(self, retry_after: float):
        # /auth is an additive API with its own rate-limit code; existing
        # /v1 codes and canonical contract constants remain unchanged.
        super().__init__(429, "rate_limited", "try again later", retry_after=retry_after)

    def response(self):
        response = super().response()
        response.headers["Retry-After"] = str(max(1, math.ceil(self.retry_after)))
        return response
