"""Narwal mainland-China cloud HTTP API.

The endpoints and request shape in this module were recovered from Narwal
Android app 2.6.90.  Callers must never log request bodies or response tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
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
        accepted_codes: frozenset[str] = frozenset({"0"}),
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
                raise NarwalCloudAuthError("Narwal cloud rejected authentication") from err
            raise NarwalCloudError("Narwal cloud request failed") from err
        except (ClientError, TimeoutError, ValueError) as err:
            raise NarwalCloudError("Narwal cloud request failed") from err

        if not isinstance(body, dict):
            raise NarwalCloudError("Narwal cloud returned an invalid response")
        code = body.get("code")
        if str(code) not in accepted_codes:
            if path.endswith("/loginByPw") or str(code) in {"401", "403"}:
                raise NarwalCloudAuthError("Narwal cloud rejected authentication")
            raise NarwalCloudError(f"Narwal cloud rejected request (code={code})")
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
            if isinstance(value, dict) and value.get("deviceId") and value.get("productId")
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
                    reset_supported=bool(_as_int(value.get("reset_btn_switch"))),
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

    async def task_records(
        self,
        *,
        device_id: str,
        product_id: str,
        map_id: int | None = None,
        pages: int = 1,
        record_limit: int = 60,
    ) -> list[dict[str, Any]]:
        """Return a bounded cleaning-task history list.

        The all-record endpoint has no query parameters. The by-map endpoint
        is the only variant that exposes ``page`` and is therefore used when
        the active map is known.
        """
        if self.account is None:
            await self.login()

        if map_id is None:
            return await self._all_task_records(
                device_id=device_id,
                product_id=product_id,
                record_limit=record_limit,
            )

        records: list[dict[str, Any]] = []
        known_ids: set[str] = set()
        previous_page_ids: tuple[str, ...] = ()
        for page in range(1, min(max(pages, 1), 5) + 1):
            try:
                result = await self._request(
                    "GET",
                    "/sweeper-map-scheme-app-server/v1/work_record/listByMap",
                    params={
                        "page": str(page),
                        # Record type 1 selects all cleaning records.
                        "type": "1",
                        "status": "0",
                        "deviceId": device_id,
                        "productId": product_id,
                        "map_id": str(map_id),
                    },
                    device_id=device_id,
                    product_id=product_id,
                )
            except NarwalCloudAuthError:
                raise
            except NarwalCloudError:
                if page == 1:
                    return await self._all_task_records(
                        device_id=device_id,
                        product_id=product_id,
                        record_limit=record_limit,
                    )
                break
            values = _task_record_values(result)
            if not values:
                break
            page_ids = tuple(
                record_id for value in values if (record_id := _task_record_id(value)) is not None
            )
            # Some older backends ignore ``page``. Avoid retaining the same
            # page repeatedly if such a device reaches this endpoint.
            if page_ids and page_ids == previous_page_ids:
                break
            previous_page_ids = page_ids
            for value in values:
                record_id = _task_record_id(value)
                if record_id is not None:
                    if record_id in known_ids:
                        continue
                    known_ids.add(record_id)
                records.append(value)
                if len(records) >= record_limit:
                    return records
        return records

    async def _all_task_records(
        self,
        *,
        device_id: str,
        product_id: str,
        record_limit: int,
    ) -> list[dict[str, Any]]:
        """Return the unpaged all-map record list."""
        result = await self._request(
            "GET",
            "/sweeper-map-scheme-app-server/v2/work_record/all",
            device_id=device_id,
            product_id=product_id,
        )
        return _task_record_values(result)[:record_limit]

    async def firmware_update(
        self,
        *,
        device_id: str,
        product_id: str,
        firmware_version: str,
    ) -> dict[str, Any]:
        """Return the read-only firmware update description."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/app-ota-server/v1/firmware/current/version/describe",
            params={
                "productId": product_id,
                "deviceId": device_id,
                "firmwareVersion": firmware_version,
            },
            device_id=device_id,
            product_id=product_id,
            # The app accepts both business codes for this response model.
            accepted_codes=frozenset({"0", "400"}),
        )
        if not isinstance(result, dict):
            raise NarwalCloudError("Firmware update response did not contain a description")
        return {
            "describe": str(result.get("describe") or ""),
            "state": _as_optional_int(result.get("state")),
            "latest_version": str(result.get("upgrade_firmware_version") or ""),
            "force_upgrade": _as_bool(result.get("force_upgrade")),
            "guide_upgrade": _as_bool(result.get("guide_upgrade")),
        }

    async def task_record_detail(
        self,
        *,
        device_id: str,
        product_id: str,
        record_id: str,
    ) -> dict[str, Any] | None:
        """Return one cleaning record detail."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/clean-record-app-server/v1/clean-record/detail",
            params={
                "clean_record_id": record_id,
                "product_id": product_id,
                "device_id": device_id,
            },
            device_id=device_id,
            product_id=product_id,
        )
        return result if isinstance(result, dict) else None

    async def dust_bag_health(
        self,
        *,
        device_id: str,
        product_id: str,
    ) -> list[dict[str, Any]]:
        """Return dust-bag health history from the app endpoint."""
        if self.account is None:
            await self.login()
        offset = datetime.now().astimezone().utcoffset()
        hours = int(offset.total_seconds() / 3600) if offset else 0
        result = await self._request(
            "GET",
            "/clean-record-app-server/v1/baseStation/getDustBagHealthDegree",
            params={
                "device_id": device_id,
                "product_id": product_id,
                "timezone": f"UTC{hours:+d}",
            },
            device_id=device_id,
            product_id=product_id,
        )
        values = (
            result
            if isinstance(result, list)
            else (result.get("list", []) if isinstance(result, dict) else [])
        )
        return [item for item in values if isinstance(item, dict)]

    async def voice_packs(
        self,
        *,
        device_id: str,
        product_id: str,
        device_language_code: str,
        app_language_code: str = "zh-CN",
    ) -> list[dict[str, str]]:
        """Return voice packages supported by the device language."""
        if self.account is None:
            await self.login()
        result = await self._request(
            "GET",
            "/business-sweeper-app-platform-server/v1/special/voice/getListByParams",
            params={
                "productId": product_id,
                "deviceLanguageCode": device_language_code,
                "appLanguageCode": app_language_code,
            },
            device_id=device_id,
            product_id=product_id,
            # VoiceService accepts both documented success response codes.
            accepted_codes=frozenset({"0", "400"}),
        )
        values = result.get("list", []) if isinstance(result, dict) else result
        if not isinstance(values, list):
            return []
        keys = (
            "name",
            "resource_id",
            "description",
            "version",
            "audition_video_url",
            "official_video_url",
            "image_url",
        )
        return [
            {key: str(value.get(key) or "") for key in keys}
            for value in values
            if isinstance(value, dict) and value.get("resource_id")
        ]


def _as_int(value: Any) -> int:
    """Convert a loosely typed cloud field to an integer."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_optional_int(value: Any) -> int | None:
    """Convert a loosely typed cloud field to an optional integer."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    """Convert the cloud's bool/int/string variants to a boolean."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _task_record_values(result: Any) -> list[dict[str, Any]]:
    """Extract a work-record list from response variants used by the backend."""
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    if not isinstance(result, dict):
        return []
    values: list[Any] = next(
        (
            result[key]
            for key in ("record_list", "work_record_list", "list", "records")
            if isinstance(result.get(key), list)
        ),
        [],
    )
    return [item for item in values if isinstance(item, dict)]


def _task_record_id(record: dict[str, Any]) -> str | None:
    """Return the stable identifier shared by record list variants."""
    value = (
        record.get("clean_record_id")
        or record.get("record_id")
        or record.get("report_id")
        or record.get("id")
    )
    return str(value) if value is not None else None
