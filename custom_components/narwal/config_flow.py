"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AREA_CODE,
    CONF_CONNECTION_TYPE,
    CONF_COUNTRY_CODE,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PRODUCT_KEY,
    CONNECTION_CLOUD,
    CONNECTION_LOCAL,
    DEFAULT_AREA_CODE,
    DEFAULT_COUNTRY_CODE,
    DEFAULT_PORT,
    DOMAIN,
    NARWAL_MODELS,
)
from .narwal_client import (
    CloudDevice,
    NarwalClient,
    NarwalCloudApi,
    NarwalCloudAuthError,
    NarwalCloudError,
)

_LOGGER = logging.getLogger(__name__)

LOCAL_MODEL_OPTIONS = (
    "Narwal Flow",
    "Narwal Flow 2",
    "Narwal Freo Z10 Ultra",
    "Narwal Freo X10 Pro",
    "Other / Auto-detect",
)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_CONNECTION_TYPE,
            default=CONNECTION_CLOUD,
        ): vol.In(
            {
                CONNECTION_CLOUD: "Narwal cloud (J3/J4/J5 and remote access)",
                CONNECTION_LOCAL: "Local WebSocket (supported LAN models)",
            }
        ),
    }
)

STEP_LOCAL_DATA_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,
        vol.Optional("port", default=DEFAULT_PORT): int,
        vol.Required(CONF_MODEL, default="Other / Auto-detect"): vol.In(
            LOCAL_MODEL_OPTIONS
        ),
    }
)

STEP_CLOUD_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PHONE): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


def _normalize_phone(phone: str, area_code: str) -> str:
    """Normalize an international-format phone for the Narwal login API."""
    normalized = phone.strip()
    for prefix in (f"+{area_code}", f"00{area_code}"):
        if normalized.startswith(prefix):
            return normalized[len(prefix) :].strip()
    return normalized


def _model_label(product_key: str) -> str:
    """Return the current model label for a product key."""
    return next(
        (
            label
            for label, configured_key in NARWAL_MODELS.items()
            if configured_key == product_key
        ),
        f"Narwal {product_key}",
    )


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a local or cloud-connected robot."""

    VERSION = 1

    def __init__(self) -> None:
        self._cloud_input: dict[str, Any] = {}
        self._cloud_devices: list[CloudDevice] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the connection transport."""
        if user_input is not None:
            if user_input[CONF_CONNECTION_TYPE] == CONNECTION_LOCAL:
                return await self.async_step_local()
            return await self.async_step_cloud()
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
        )

    async def async_step_local(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Configure direct LAN WebSocket access."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input["host"]
            port = user_input.get("port", DEFAULT_PORT)
            model_label = user_input[CONF_MODEL]
            product_key = NARWAL_MODELS[model_label]
            topic_prefix = None if product_key == "auto" else f"/{product_key}"
            client = NarwalClient(host=host, port=port, topic_prefix=topic_prefix)
            try:
                await client.connect()
                await client.discover_device_id(timeout=15.0)
                await client.drain_ws_buffer()
                device_info = await client.get_device_info()
            except Exception as err:
                _LOGGER.warning("Local Narwal setup failed: %s", type(err).__name__)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(device_info.device_id)
                self._abort_if_unique_id_configured()
                resolved_key = client.topic_prefix.lstrip("/")
                resolved_model = (
                    model_label
                    if product_key != "auto"
                    else _model_label(resolved_key)
                )
                return self.async_create_entry(
                    title=resolved_model,
                    data={
                        CONF_CONNECTION_TYPE: CONNECTION_LOCAL,
                        "host": host,
                        "port": port,
                        "device_id": device_info.device_id,
                        CONF_PRODUCT_KEY: resolved_key,
                        CONF_MODEL: resolved_model,
                    },
                )
            finally:
                await client.disconnect()
        return self.async_show_form(
            step_id="local",
            data_schema=STEP_LOCAL_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate and discover cloud-bound robots."""
        errors: dict[str, str] = {}
        if user_input is not None:
            area_code = DEFAULT_AREA_CODE
            country_code = DEFAULT_COUNTRY_CODE
            phone = _normalize_phone(user_input[CONF_PHONE], area_code)
            api = NarwalCloudApi(
                async_get_clientsession(self.hass),
                phone=phone,
                password=user_input[CONF_PASSWORD],
                area_code=area_code,
                country_code=country_code,
            )
            try:
                await api.login()
                devices = await api.devices()
                await api.broker()
            except NarwalCloudAuthError:
                errors["base"] = "invalid_auth"
            except NarwalCloudError:
                errors["base"] = "cannot_connect"
            else:
                if not devices:
                    errors["base"] = "no_devices"
                else:
                    self._cloud_input = {
                        **user_input,
                        CONF_PHONE: phone,
                        CONF_AREA_CODE: area_code,
                        CONF_COUNTRY_CODE: country_code,
                    }
                    self._cloud_devices = devices
                    if len(devices) == 1:
                        return await self._create_cloud_entry(devices[0])
                    return await self.async_step_cloud_device()
        return self.async_show_form(
            step_id="cloud",
            data_schema=STEP_CLOUD_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one robot when an account contains multiple devices."""
        choices = {
            device.device_id: (
                f"{device.name} ({_model_label(device.product_id)})"
            )
            for device in self._cloud_devices
        }
        if user_input is not None:
            selected = next(
                item
                for item in self._cloud_devices
                if item.device_id == user_input["device_id"]
            )
            return await self._create_cloud_entry(selected)
        return self.async_show_form(
            step_id="cloud_device",
            data_schema=vol.Schema(
                {vol.Required("device_id"): vol.In(choices)}
            ),
        )

    async def _create_cloud_entry(self, device: CloudDevice) -> ConfigFlowResult:
        await self.async_set_unique_id(device.device_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=device.name or f"Narwal {device.product_id}",
            data={
                CONF_CONNECTION_TYPE: CONNECTION_CLOUD,
                CONF_PHONE: self._cloud_input[CONF_PHONE].strip(),
                CONF_PASSWORD: self._cloud_input[CONF_PASSWORD],
                CONF_AREA_CODE: self._cloud_input[CONF_AREA_CODE],
                CONF_COUNTRY_CODE: self._cloud_input[
                    CONF_COUNTRY_CODE
                ].upper(),
                "device_id": device.device_id,
                CONF_PRODUCT_KEY: device.product_id,
                CONF_MODEL: _model_label(device.product_id),
            },
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for an existing cloud entry."""
        self._cloud_input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement cloud credentials and reload the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            area_code = str(self._cloud_input[CONF_AREA_CODE]).strip()
            country_code = str(
                self._cloud_input[CONF_COUNTRY_CODE]
            ).strip().upper()
            updated = {
                **self._cloud_input,
                CONF_PHONE: _normalize_phone(
                    user_input[CONF_PHONE], area_code
                ),
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_AREA_CODE: area_code,
                CONF_COUNTRY_CODE: country_code,
            }
            api = NarwalCloudApi(
                async_get_clientsession(self.hass),
                phone=updated[CONF_PHONE],
                password=updated[CONF_PASSWORD],
                area_code=updated[CONF_AREA_CODE],
                country_code=updated[CONF_COUNTRY_CODE],
            )
            try:
                await api.login()
                devices = await api.devices()
            except NarwalCloudAuthError:
                errors["base"] = "invalid_auth"
            except NarwalCloudError:
                errors["base"] = "cannot_connect"
            else:
                if not any(
                    item.device_id == updated["device_id"] for item in devices
                ):
                    errors["base"] = "device_not_found"
                else:
                    return self.async_update_reload_and_abort(
                        self._get_reauth_entry(),
                        data_updates=updated,
                    )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PHONE,
                        default=self._cloud_input[CONF_PHONE],
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )
