"""Tests for api/middleware/rate_limiter.py"""

from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.middleware.rate_limiter import RateLimiterMiddleware


def build_app(rate_limiter=None, limit=60, initialized=True):
    """Build a minimal app with the middleware and a stub route."""
    app = FastAPI()
    app.add_middleware(RateLimiterMiddleware)

    route_calls = Mock()

    @app.get("/ping")
    def ping():
        route_calls()
        return {"ok": True}

    if initialized:
        app.state.rate_limiter = rate_limiter
        app.state.rate_limit_per_minute = limit

    app.state.route_calls = route_calls
    return app


@pytest.mark.unit
class TestRateLimiterMiddleware:
    """Test suite for RateLimiterMiddleware."""

    def test_allowed_request_gets_rate_limit_headers(self):
        limiter = Mock()
        limiter.check_rate_limit.return_value = (True, 59)
        client = TestClient(build_app(limiter, limit=60))

        response = client.get("/ping")

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        assert response.headers["X-RateLimit-Limit"] == "60"
        assert response.headers["X-RateLimit-Remaining"] == "59"

    def test_denied_request_returns_429_without_calling_route(self):
        limiter = Mock()
        limiter.check_rate_limit.return_value = (False, 0)
        app = build_app(limiter, limit=60)
        client = TestClient(app)

        response = client.get("/ping")

        assert response.status_code == 429
        assert response.json() == {"detail": "Rate limit exceeded"}
        assert response.headers["X-RateLimit-Limit"] == "60"
        assert response.headers["X-RateLimit-Remaining"] == "0"
        app.state.route_calls.assert_not_called()

    def test_check_failure_fails_open(self):
        limiter = Mock()
        limiter.check_rate_limit.side_effect = RuntimeError("redis down")
        client = TestClient(build_app(limiter, limit=60))

        response = client.get("/ping")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "60"
        assert response.headers["X-RateLimit-Remaining"] == "60"

    def test_uninitialized_limiter_skips_rate_limiting(self):
        client = TestClient(build_app(initialized=False))

        response = client.get("/ping")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers
        assert "X-RateLimit-Remaining" not in response.headers

    def test_null_limiter_from_failed_startup_skips_rate_limiting(self):
        # Startup sets rate_limiter to None when Redis is unreachable; the
        # limit setting is then never stored, so the middleware must skip.
        app = FastAPI()
        app.add_middleware(RateLimiterMiddleware)

        @app.get("/ping")
        def ping():
            return {"ok": True}

        app.state.rate_limiter = None
        client = TestClient(app)

        response = client.get("/ping")

        assert response.status_code == 200
        assert "X-RateLimit-Limit" not in response.headers

    def test_uses_first_forwarded_for_address_as_identifier(self):
        limiter = Mock()
        limiter.check_rate_limit.return_value = (True, 59)
        client = TestClient(build_app(limiter, limit=60))

        client.get("/ping", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"})

        identifier = limiter.check_rate_limit.call_args.args[0]
        assert identifier == "203.0.113.7"


@pytest.mark.unit
class TestRateLimiterWithCORS:
    """Rate limit responses must work with the real app's CORS setup."""

    ORIGIN = "http://localhost:5173"

    @pytest.fixture
    def real_app(self, monkeypatch):
        from api.main import app

        limiter = Mock()
        monkeypatch.setattr(app.state, "rate_limiter", limiter, raising=False)
        monkeypatch.setattr(app.state, "rate_limit_per_minute", 60, raising=False)
        return app, limiter

    def test_429_response_carries_cors_headers(self, real_app):
        app, limiter = real_app
        limiter.check_rate_limit.return_value = (False, 0)
        client = TestClient(app)

        response = client.get("/", headers={"Origin": self.ORIGIN})

        assert response.status_code == 429
        assert response.headers["access-control-allow-origin"] == self.ORIGIN
        assert response.headers["X-RateLimit-Remaining"] == "0"

    def test_allowed_response_exposes_rate_limit_headers(self, real_app):
        app, limiter = real_app
        limiter.check_rate_limit.return_value = (True, 59)
        client = TestClient(app)

        response = client.get("/", headers={"Origin": self.ORIGIN})

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == self.ORIGIN
        exposed = response.headers["access-control-expose-headers"]
        assert "X-RateLimit-Limit" in exposed
        assert "X-RateLimit-Remaining" in exposed
