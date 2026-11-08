"""Tests for Narwal cloud HTTP request contracts and authentication recovery."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    NarwalCloudApi,
    NarwalCloudError,
)


class _Response:
    def __init__(self, body: object) -> None:
        self._body = body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        del content_type
        return self._body


class _Session:
    """Fake aiohttp session returning sequential response envelopes."""

    def __init__(self, *bodies: object) -> None:
        self._bodies = list(bodies)
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        return _Response(self._bodies.pop(0))


def _api(session: object | None = None) -> NarwalCloudApi:
    phone = "13800000000"
    return NarwalCloudApi(
        session or _Session({}),
        phone=phone,
        password=phone[:0],
    )


def test_expired_token_relogs_in_and_retries_request() -> None:
    """An expired business token is refreshed once and the request retried."""
    session = _Session(
        {"code": "130105", "result": None},
        {"code": "0", "result": {"token": "new", "uuid": "user"}},
        {
            "code": "0",
            "result": {"deviceInfoList": [{"deviceId": "device", "productId": "product"}]},
        },
    )
    api = _api(session)
    api.account = CloudAccount(token="old", uuid="user")

    devices = asyncio.run(api.devices())

    assert [device.device_id for device in devices] == ["device"]
    assert api.account == CloudAccount(token="new", uuid="user")
    assert session.calls[0][2]["headers"]["Auth-Token"] == "old"
    assert session.calls[2][2]["headers"]["Auth-Token"] == "new"
    assert session._bodies == []


def test_zero_padded_success_code_is_accepted() -> None:
    """The cloud reports success as a zero-padded business code."""
    session = _Session(
        {
            "code": "000",
            "result": {"deviceInfoList": [{"deviceId": "device", "productId": "product"}]},
        }
    )
    api = _api(session)
    api.account = CloudAccount(token="token", uuid="user")

    devices = asyncio.run(api.devices())

    assert [device.device_id for device in devices] == ["device"]


def test_notification_request_matches_app_endpoint_and_query() -> None:
    """Preserve the App's oneDeviceListPage path and four query fields."""
    session = _Session({"code": "0", "result": {"message_list": []}})
    api = _api(session)
    api.account = CloudAccount(token="token", uuid="user")

    asyncio.run(api.notifications(device_id="device", product_id="product", page_count=12))

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/app-message-server/v1/device-message/oneDeviceListPage")
    assert kwargs["params"] == {
        "device_id": "device",
        "page": "1",
        "page_count": "12",
        "app_language": "zh_CN",
    }


def test_current_dust_bag_request_matches_app_endpoint_and_query() -> None:
    """Preserve the App's endpoint and device context for dust-bag health."""
    session = _Session({"code": "0", "result": {"health_degree": 88, "day": 12}})
    api = _api(session)
    api.account = CloudAccount(token="token", uuid="user")

    asyncio.run(api.dust_bag_health_current(device_id="device", product_id="product"))

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/clean-record-app-server/v1/baseStation/getDustBagHealthDegreeAndDay")
    assert kwargs["params"] == {"device_id": "device", "product_id": "product"}


def test_notification_string_boolean_is_normalized() -> None:
    """The cloud reports read state as strings; unread messages stay visible."""
    session = _Session(
        {
            "code": "0",
            "result": {
                "message_list": [
                    {"msg_id": "1", "is_read": "false"},
                    {"msg_id": "2", "is_read": "true"},
                ]
            },
        }
    )
    api = _api(session)
    api.account = CloudAccount(token="token", uuid="user")

    messages = asyncio.run(api.notifications(device_id="device", product_id="product"))

    assert [message.is_read for message in messages] == [False, True]


@pytest.mark.parametrize("result", [None, [None]])
def test_consumables_reject_damaged_cloud_responses(result: object) -> None:
    """A damaged external response must not look like an empty consumable list."""
    api = _api(_Session({"code": "0", "result": result}))
    api.account = CloudAccount(token="token", uuid="user")

    with pytest.raises(NarwalCloudError, match="Consumables list response was invalid"):
        asyncio.run(api.consumables(device_id="device", product_id="product"))
