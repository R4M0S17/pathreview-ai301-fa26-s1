import structlog
from fastapi import Request
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response

log = structlog.get_logger()


class RateLimiterMiddleware(BaseHTTPMiddleware):
    """
    Middleware that enforces rate limiting on incoming requests,
    sets rate limit headers, and returns 429 when limits are exceeded.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Extract client IP address
        client_ip = request.client.host if request.client else "unknown"
        if forwarded_for := request.headers.get("X-Forwarded-For"):
            client_ip = forwarded_for.split(",")[0].strip()

        # Get rate limiter and settings from app state
        try:
            rate_limiter = request.app.state.rate_limiter
            limit = request.app.state.rate_limit_per_minute
            window_seconds = 60
        except AttributeError:
            # If rate limiter not initialized, skip rate limiting
            log.warning("rate_limiter_not_initialized")
            response = await call_next(request)
            return response

        # Check rate limit. The Redis client is synchronous, so run the check in a
        # worker thread to avoid blocking the event loop on every request.
        try:
            allowed, remaining = await run_in_threadpool(
                rate_limiter.check_rate_limit,
                client_ip,
                limit=limit,
                window_seconds=window_seconds,
            )
        except Exception as e:
            log.error("rate_limiter_check_failed", error=str(e))
            # Fail open on error
            allowed = True
            remaining = limit

        # Return 429 if limit exceeded
        if not allowed:
            log.info("rate_limit_exceeded", client_ip=client_ip)
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                },
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers
        response.headers["X-RateLimit-Limit"] = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)

        return response
