"""Narwal mainland-China cloud HTTP API.

The endpoints and request shape in this module were recovered from Narwal
Android app 2.6.90.  Callers must never log request bodies or response tokens.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import ClientError, ClientResponseError, ClientSession

from .models import ConsumableInfo, NarwalNotification

APP_VERSION = "2.6.90"
VERSION_CODE = "2137"
DEFAULT_BASE_URL = "https://cn-app.narwaltech.com"


class NarwalCloudError(Exception):
    """Base error returned by the Narwal cloud."""


class NarwalCloudAuthError(NarwalCloudError):
    """Authentication was rejected."""


@dataclass(frozen=True, slots=True)
class CloudAccount:
    """Authenticated account details needed by MQTT."""

    token: str = field(repr=False)
    uuid: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class CloudDevice:
    """A robot returned by the cloud device-list endpoint."""

    device_id: str = field(repr=False)
    product_id: str
    name: str = field(repr=False)
    firmware_version: str
    serial_number: str = field(repr=False)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> CloudDevice:
        """Create a sanitized device model from an API response."""
        return cls(
            device_id=str(data.get("deviceId", "")),
            product_id=str(data.get("productId", "")),
            name=str(data.get("robotName", "") or "Narwal"),
            firmware_version=str(data.get("firmwareVersion", "")),
            serial_number=str(data.get("snNumber", "")),
        )


class NarwalCloudApi:
    """Small async client for login, inventory, and broker discovery."""

    def __init__(
        self,
        session: ClientSession,
        *,
        phone: str,
        password: str,
        area_code: str = "86",
        country_code: str = "CN",
        base_url: str = DEFAULT_BASE_URL,
    ) -> None:
        self._session = session
        self._phone = phone
        self._password = password
        self._area_code = area_code
        self._country_code = country_code.upper()
        self._base_url = base_url.rstrip("/")
        self.account: CloudAccount | None = None

    def _headers(
        self,
        *,
        device_id: str = "",
        product_id: str = "",
    ) -> dict[str, str]:
        account = self.account
        return {
            "APP-VERSION": APP_VERSION,
            "version_code": VERSION_CODE,
            "APP-LANGUAGE": "zh_CN",
            "is_app": "true",
            "app_version": APP_VERSION,
            "country_code": self._country_code,
            "language_code": "zh_CN",
            "did": device_id,
            "Authorization": "",
            "Aiot-Application-Id": product_id,
            "isMainland": "true",
            "is_beta": "false",
            "app_type": "normal",
            "x-b3-traceid": "",
            "platform": "android",
            "Auth-Token": account.token if account else "",
            "UUID": account.uuid if account else "",
        }

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        device_id: str = "",
        product_id: str = "",
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                params=params,
                headers=self._headers(
                    device_id=device_id,
                    product_id=product_id,
                ),
                timeout=25,
            ) as response:
                response.raise_for_status()
                body = await response.json(content_type=None)
        except ClientResponseError as err:
            if err.status in (401, 403):
                raise NarwalCloudAuthError(
                    "Narwal cloud rejected authentication"
                ) from err
            raise NarwalCloudError("Narwal cloud request failed") from err
        except (ClientError, TimeoutError, ValueError) as err:
            raise NarwalCloudError("Narwal cloud request failed") from err

        if not isinstance(body, dict):
            raise NarwalCloudError("Narwal cloud returned an invalid response")
        code = body.get("code")
        if code not in (0, "0"):
            if path.endswith("/loginByPw") or str(code) in {"401", "403"}:
                raise NarwalCloudAuthError(
                    "Narwal cloud rejected authentication"
                )
            raise NarwalCloudError(
                f"Narwal cloud rejected request (code={code})"
            )
        result = body.get("result")
        return body.get("data") if result is None and "data" in body else result

    async def login(self) -> CloudAccount:
        """Authenticate with the same password flow used by the Android app."""
        result = await self._request(
            "POST",
            "/user-authentication-server/v2/login/loginByPw",
            json={
                "area_code": self._area_code,
                "mobile": self._phone,
                "password": self._password,
                "last_login_system": 3,
                "last_login_app_version": APP_VERSION,
                "terminal_brand": "Home Assistant",
                "terminal_device_name": "Home Assistant",
                "terminal_model": "Home Assistant",
                "terminal_version": "Python",
                "terminal_system_name": "Linux",
                "terminal_system_version": "",
                "terminal_system_language": "zh_CN",
                "terminal_display": "",
                "app_version": APP_VERSION,
                "app_language": "zh_CN",
                "source": 1,
                "user_agent": "Home Assistant Narwal",
                "captcha_code": "",
            },
        )
        if not isinstance(result, dict):
            raise NarwalCloudAuthError("Login response did not contain an account")
        token = str(result.get("token", ""))
        user_uuid = str(result.get("uuid", ""))
        if not token or not user_uuid:
            raise NarwalCloudAuthError("Login response did not contain credentials")
        self.account = CloudAccount(token=token, uuid=user_uuid)
        return self.account

    async def devices(self) -> list[CloudDevice]:
        """Return robots bound to the authenticated account."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/user-device-platform-server/device-info/getDeviceInfoList",
        )
        if not isinstance(result, dict):
            return []
        values = result.get("deviceInfoList")
        if not isinstance(values, list):
            return []
        return [
            CloudDevice.from_json(value)
            for value in values
            if isinstance(value, dict)
            and value.get("deviceId")
            and value.get("productId")
        ]

    async def broker(self) -> str:
        """Return the MQTT-over-TLS broker hostname."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/iot-broker-discover/app/v1/broker/discover",
            params={"country": self._country_code},
        )
        if not isinstance(result, str) or not result:
            raise NarwalCloudError("Broker discovery returned no endpoint")
        return result

    async def consumables(
        self,
        *,
        device_id: str,
        product_id: str,
    ) -> list[ConsumableInfo]:
        """Return consumable lifetimes using the app's v3 list endpoint."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "POST",
            "/consumables-management-app-server/v3/consumables/list",
            json={
                "product_id": product_id,
                "device_id": device_id,
                "language": "zh_CN",
                "is_up_down_water": 0,
            },
            device_id=device_id,
            product_id=product_id,
        )
        if isinstance(result, dict):
            values = next(
                (
                    result[key]
                    for key in (
                        "consumable_list",
                        "consumables",
                        "list",
                        "data_list",
                    )
                    if isinstance(result.get(key), list)
                ),
                [],
            )
        else:
            values = result if isinstance(result, list) else []

        consumables: list[ConsumableInfo] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            type_id = _as_int(value.get("type"))
            code = str(value.get("consumables_code") or "")
            key = code or f"type_{type_id}"
            consumables.append(
                ConsumableInfo(
                    key=key,
                    name=str(value.get("name") or f"Consumable {type_id}"),
                    type_id=type_id,
                    category=_as_int(value.get("item_type")),
                    total_duration=_as_int(value.get("total_duration")),
                    usage_duration=_as_int(value.get("usage_duration")),
                    subtitle=str(value.get("subtitle") or ""),
                    code=code,
                )
            )
        return consumables

    async def notifications(
        self,
        *,
        device_id: str,
        product_id: str,
        page_count: int = 20,
    ) -> list[NarwalNotification]:
        """Return the latest device messages from the Narwal cloud."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/app-message-server/v1/device-message/oneDeviceListPage",
            params={
                "device_id": device_id,
                "page": "1",
                "page_count": str(page_count),
                "app_language": "zh_CN",
            },
            device_id=device_id,
            product_id=product_id,
        )
        values = result.get("message_list", []) if isinstance(result, dict) else []
        if not isinstance(values, list):
            return []
        messages: list[NarwalNotification] = []
        for value in values:
            if not isinstance(value, dict):
                continue
            message_id = str(value.get("msg_id") or value.get("id") or "")
            if not message_id:
                continue
            messages.append(
                NarwalNotification(
                    message_id=message_id,
                    title=str(value.get("msg_title") or ""),
                    content=str(value.get("msg_content") or ""),
                    created_at=_as_int(value.get("created_time")),
                    is_read=bool(value.get("is_read", False)),
                    device_id=str(value.get("device_id") or device_id),
                )
            )
        return messages

    async def latest_task_record(
        self,
        *,
        device_id: str,
        product_id: str,
    ) -> dict[str, Any] | None:
        """Return the latest cleaning record using the app's v2 endpoint."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/sweeper-map-scheme-app-server/v2/work_record/getLatestRecord",
            params={
                "deviceId": device_id,
                "productId": product_id,
                "requestTime": str(int(time.time())),
            },
            device_id=device_id,
            product_id=product_id,
        )
        return result if isinstance(result, dict) else None


def _as_int(value: Any) -> int:
    """Convert a loosely typed cloud field to an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
