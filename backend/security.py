"""Transport-level hardening: security headers, rate limiting, and body-size caps.

None of this is a substitute for authentication - see auth.py and the README's
security section for what these controls do and do not cover.
"""
import time
from collections import defaultdict, deque

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

# Uploads are read fully into memory before they reach the model, so an unbounded
# body is a trivial way to exhaust the process. A downscaled screenshot from the
# extension is ~15KB; 8MB is generous for anything legitimate.
MAX_BODY_BYTES = 8 * 1024 * 1024

SECURITY_HEADERS = {
    # The API returns JSON and PNGs and never renders HTML, so it can afford the
    # strictest possible policy.
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), interest-cohort=()",
    "Cross-Origin-Resource-Policy": "same-site",
}


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject oversized bodies up front, before anything buffers them."""

    async def dispatch(self, request: Request, call_next):
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            return JSONResponse(
                status_code=413,
                content={"type": "error",
                         "message": f"That file is too large. The limit is {MAX_BODY_BYTES // (1024*1024)}MB."},
            )
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window-per-caller limiter.

    In-memory, so it resets on restart and does not span multiple workers. That is
    the right trade for a single-process localhost service: it stops a runaway
    extension or a script hammering the API, which is the realistic failure here,
    without adding a Redis dependency. A public deployment would need a shared
    store.

    AI-backed routes get a much tighter budget than reads, because each one costs
    real quota.
    """

    AI_ROUTES = ("/analyze", "/analyze-image", "/analyze-audio",
                 "/analyze-video", "/analyze-website", "/chat", "/chat-file")

    def __init__(self, app, read_limit=120, ai_limit=40, window=60):
        super().__init__(app)
        self.read_limit = read_limit
        self.ai_limit = ai_limit
        self.window = window
        self._hits = defaultdict(deque)

    def _client(self, request: Request) -> str:
        return request.client.host if request.client else "unknown"

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ("/health", "/docs", "/openapi.json"):
            return await call_next(request)

        is_ai = any(path.startswith(r) for r in self.AI_ROUTES)
        limit = self.ai_limit if is_ai else self.read_limit
        key = f"{self._client(request)}:{'ai' if is_ai else 'read'}"

        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()

        if len(hits) >= limit:
            retry = int(self.window - (now - hits[0])) + 1
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry)},
                content={"type": "error",
                         "message": f"Too many requests. Try again in {retry} seconds."},
            )

        hits.append(now)
        return await call_next(request)
