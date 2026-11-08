"""Regression tests for untrusted Narwal cloud HTTP response boundaries."""

from __future__ import annotations

import asyncio
import traceback
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    NarwalCloudApi,
    NarwalCloudAuthError,
    NarwalCloudError,
)


class _Response:
    def __init__(self, body: object = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        if self._error is not None:
            raise self._error

    async def json(self, *, content_type: object = None) -> object:
        del content_type
        return self._body


class _Session:
    """Fake aiohttp session returning one response envelope."""

    def __init__(self, body: object = None, error: Exception | None = None) -> None:
        self._body = body
        self._error = error

    def request(self, *_args: object, **_kwargs: object) -> _Response:
        return _Response(self._body, self._error)


def _api(session: object | None = None) -> NarwalCloudApi:
    """Return an API instance whose transport is replaced by each test."""
    return NarwalCloudApi(
        session or object(),  # type: ignore[arg-type]
        phone="13800000000",
        password="secret",
    )


def _authenticated_api(session: _Session) -> NarwalCloudApi:
    api = _api(session)
    api.account = CloudAccount(token="token", uuid="uuid")
    return api


@pytest.mark.parametrize(
    ("token", "user_uuid"),
    [(None, "uuid"), ("token", "   ")],
)
def test_login_rejects_missing_credentials(token: object, user_uuid: object) -> None:
    """JSON null or blank text must never become a truthy cloud credential."""
    session = _Session({"code": "0", "result": {"token": token, "uuid": user_uuid}})

    with pytest.raises(NarwalCloudAuthError):
        asyncio.run(_api(session).login())


@pytest.mark.parametrize("result", [None, {"deviceInfoList": None}])
def test_devices_rejects_malformed_list_envelopes(result: object) -> None:
    """A damaged response is a cloud failure, not a valid empty account."""
    api = _authenticated_api(_Session({"code": "0", "result": result}))

    with pytest.raises(NarwalCloudError, match="Device list response was invalid"):
        asyncio.run(api.devices())


def test_devices_ignores_duplicate_and_malformed_entries() -> None:
    """Only well-formed robots are exposed to the device selection flow."""
    api = _authenticated_api(
        _Session(
            {
                "code": "0",
                "result": {
                    "deviceInfoList": [
                        {"deviceId": "device", "productId": "product"},
                        {"deviceId": " device ", "productId": " product "},
                        {"deviceId": None, "productId": "product"},
                        "invalid",
                    ]
                },
            }
        )
    )

    devices = asyncio.run(api.devices())

    assert [(item.device_id, item.product_id) for item in devices] == [("device", "product")]


@pytest.mark.parametrize("endpoint", ["", "  "])
def test_broker_rejects_blank_endpoints(endpoint: str) -> None:
    """Whitespace-only broker responses cannot reach the MQTT parser."""
    api = _authenticated_api(_Session({"code": "0", "result": endpoint}))

    with pytest.raises(NarwalCloudError, match="no endpoint"):
        asyncio.run(api.broker())


def test_broker_strips_transport_whitespace() -> None:
    """A valid endpoint is normalized before it reaches MQTT setup."""
    api = _authenticated_api(_Session({"code": "0", "result": "  mqtts://broker.example:8883  "}))

    assert asyncio.run(api.broker()) == "mqtts://broker.example:8883"


def test_business_error_does_not_echo_vendor_controlled_code() -> None:
    """Cloud response strings are not copied into user-visible/logged errors."""
    secret = "credential-like-value"
    api = _authenticated_api(_Session({"code": secret, "result": None}))

    with pytest.raises(NarwalCloudError) as raised:
        asyncio.run(api.devices())

    assert secret not in str(raised.value)


def test_http_error_suppresses_sensitive_url_context() -> None:
    """A transport traceback must not render device IDs from query strings."""
    secret = "sensitive-device-id"
    transport_error = ClientResponseError(
        SimpleNamespace(real_url=f"https://example.invalid/api?device_id={secret}"),
        (),
        status=500,
        message="failure",
    )
    api = _authenticated_api(_Session(error=transport_error))

    with pytest.raises(NarwalCloudError) as raised:
        asyncio.run(api.devices())

    formatted_traceback = "".join(
        traceback.format_exception(raised.type, raised.value, raised.value.__traceback__)
    )
    assert secret not in formatted_traceback
