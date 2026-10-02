"""Tests for aiophyn authentication logic (issues #56 / #60).

Verifies that:
- async_authenticate prefers the REFRESH_TOKEN_AUTH flow when a refresh token
  is available, avoiding unnecessary SRP round-trips.
- It falls back to full SRP when the refresh token is expired / invalid, and
  clears the stale token so the next call goes straight to SRP.
- _apply_auth_result preserves an existing refresh token when the Cognito
  response does not include a new one (REFRESH_TOKEN_AUTH never returns one).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aiophyn.api import API
from botocore.exceptions import ClientError as BotocoreClientError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_api(refresh_token: str | None = "rt-existing") -> API:
    """Return an API instance with a minimal fake session and pre-set tokens."""
    api = API.__new__(API)
    api._username = "test@example.com"
    api._password = "password"
    api._cognito = {
        "app_client_id": "client-id",
        "pool_id": "pool-id",
        "region": "us-east-1",
    }
    api._session = None
    api._iot_id = None
    api._iot_credentials = None
    api.mqtt = None
    api._id_token = None
    api._refresh_token = refresh_token
    api.verify_ssl = True
    api.proxy = None
    api.proxy_port = None
    api.proxy_url = None
    api._token = None
    api._token_expiration = None
    return api


def _auth_result(*, include_refresh: bool = True) -> dict:
    """Fake Cognito AuthenticationResult response."""
    res: dict = {
        "AccessToken": "new-access-token",
        "ExpiresIn": 3600,
        "IdToken": "new-id-token",
        "TokenType": "Bearer",
    }
    if include_refresh:
        res["RefreshToken"] = "new-refresh-token"
    return {"AuthenticationResult": res}


# ---------------------------------------------------------------------------
# _apply_auth_result
# ---------------------------------------------------------------------------

class TestApplyAuthResult:
    def test_stores_tokens(self):
        api = _make_api(refresh_token=None)
        api._apply_auth_result(_auth_result(include_refresh=True))
        assert api._token == "new-access-token"
        assert api._id_token == "new-id-token"
        assert api._refresh_token == "new-refresh-token"
        assert api._token_expiration is not None

    def test_preserves_existing_refresh_token_when_absent(self):
        """REFRESH_TOKEN_AUTH never returns a RefreshToken — must keep the old one."""
        api = _make_api(refresh_token="rt-existing")
        api._apply_auth_result(_auth_result(include_refresh=False))
        assert api._refresh_token == "rt-existing"

    def test_overwrites_refresh_token_when_present(self):
        api = _make_api(refresh_token="rt-old")
        api._apply_auth_result(_auth_result(include_refresh=True))
        assert api._refresh_token == "new-refresh-token"


# ---------------------------------------------------------------------------
# async_authenticate — refresh-first, SRP fallback
# ---------------------------------------------------------------------------

class TestAsyncAuthenticate:
    def test_uses_refresh_token_when_available(self):
        """With a valid refresh token, no SRP call should be made."""
        api = _make_api(refresh_token="rt-existing")
        refresh_result = _auth_result(include_refresh=False)
        srp_called = []

        async def fake_run_blocking(fn):
            if fn == api._authenticate:
                srp_called.append(True)
            return refresh_result

        api._run_blocking = fake_run_blocking

        asyncio.run(api.async_authenticate())

        assert not srp_called, "SRP should not be called when refresh token is available"
        assert api._refresh_token == "rt-existing"  # preserved (no new one returned)
        assert api._token == "new-access-token"

    def test_falls_back_to_srp_when_refresh_fails(self):
        """Expired refresh token → clears it, then falls back to SRP."""
        api = _make_api(refresh_token="rt-expired")
        srp_result = _auth_result(include_refresh=True)

        botocore_err = BotocoreClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "Expired"}},
            "InitiateAuth",
        )

        async def fake_run_blocking(fn):
            if fn == api._refresh_token_auth:
                raise botocore_err
            return srp_result

        api._run_blocking = fake_run_blocking

        asyncio.run(api.async_authenticate())

        # Stale refresh token must be cleared; SRP result applied (includes new rt)
        assert api._refresh_token == "new-refresh-token"
        assert api._token == "new-access-token"

    def test_skips_refresh_when_no_refresh_token(self):
        """No refresh token → straight to SRP, no attempt at REFRESH_TOKEN_AUTH."""
        api = _make_api(refresh_token=None)
        srp_result = _auth_result(include_refresh=True)
        called = []

        async def fake_run_blocking(fn):
            called.append(getattr(fn, "__name__", repr(fn)))
            return srp_result

        api._run_blocking = fake_run_blocking

        asyncio.run(api.async_authenticate())

        assert api._token == "new-access-token"
        assert "_refresh_token_auth" not in called

    def test_allow_refresh_false_skips_refresh(self):
        """allow_refresh=False must bypass the refresh flow even if token exists."""
        api = _make_api(refresh_token="rt-existing")
        srp_result = _auth_result(include_refresh=True)
        called = []

        async def fake_run_blocking(fn):
            called.append(getattr(fn, "__name__", repr(fn)))
            return srp_result

        api._run_blocking = fake_run_blocking

        asyncio.run(api.async_authenticate(allow_refresh=False))

        assert "_refresh_token_auth" not in called
        assert api._token == "new-access-token"


# ---------------------------------------------------------------------------
# async_authenticate — transport failures (aiophyn PR #7)
# ---------------------------------------------------------------------------

from botocore.exceptions import EndpointConnectionError

from aiophyn.errors import AuthenticationError, RequestError


class TestAuthenticateTransportErrors:
    def test_srp_transport_failure_becomes_request_error(self):
        """A network outage during SRP login must surface as RequestError so
        callers can treat it as transient (ConfigEntryNotReady in HA)."""
        api = _make_api(refresh_token=None)

        async def fake_run_blocking(fn):
            raise EndpointConnectionError(endpoint_url="https://cognito")

        api._run_blocking = fake_run_blocking

        with pytest.raises(RequestError):
            asyncio.run(api.async_authenticate())

    def test_refresh_transport_failure_becomes_request_error_and_keeps_token(self):
        """Transport failure on the refresh path must not fall through to SRP
        and must keep the refresh token for the next attempt."""
        api = _make_api(refresh_token="rt-existing")
        srp_called = []

        async def fake_run_blocking(fn):
            if fn == api._authenticate:
                srp_called.append(True)
            raise EndpointConnectionError(endpoint_url="https://cognito")

        api._run_blocking = fake_run_blocking

        with pytest.raises(RequestError):
            asyncio.run(api.async_authenticate())

        assert not srp_called
        assert api._refresh_token == "rt-existing"

    def test_bad_credentials_still_authentication_error(self):
        api = _make_api(refresh_token=None)
        err = BotocoreClientError(
            {"Error": {"Code": "NotAuthorizedException", "Message": "bad"}},
            "InitiateAuth",
        )

        async def fake_run_blocking(fn):
            raise err

        api._run_blocking = fake_run_blocking

        with pytest.raises(AuthenticationError):
            asyncio.run(api.async_authenticate())


# ---------------------------------------------------------------------------
# _run_blocking — bounded wait (aiophyn #4)
# ---------------------------------------------------------------------------

import aiophyn.api as api_module


class TestRunBlockingTimeout:
    def test_hung_blocking_call_raises_request_error(self, monkeypatch):
        """A Cognito call that never returns must not hang the caller forever."""
        monkeypatch.setattr(api_module, "AUTH_TIMEOUT", 0.05)
        api = _make_api(refresh_token=None)

        import threading
        release = threading.Event()

        def hung_authenticate():
            release.wait(2)
            return _auth_result()

        api._authenticate = hung_authenticate
        try:
            with pytest.raises(RequestError):
                asyncio.run(asyncio.wait_for(api.async_authenticate(), timeout=1))
        finally:
            release.set()

    def test_fast_blocking_call_returns_result(self):
        api = _make_api(refresh_token=None)
        api._authenticate = lambda: _auth_result()

        asyncio.run(api.async_authenticate())

        assert api._token == "new-access-token"
