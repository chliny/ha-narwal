"""Config flow for Narwal vacuum integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AREA_CODE,
    CONF_COUNTRY_CODE,
    CONF_MODEL,
    CONF_PASSWORD,
    CONF_PHONE,
    CONF_PRODUCT_KEY,
    DEFAULT_AREA_CODE,
    DEFAULT_COUNTRY_CODE,
    DOMAIN,
    NARWAL_MODELS,
)
from .narwal_client import (
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudAuthError,
    NarwalCloudError,
)

STEP_USER_DATA_SCHEMA = vol.Schema(
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
        (label for label, configured_key in NARWAL_MODELS.items() if configured_key == product_key),
        f"Narwal {product_key}",
    )


class NarwalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the cloud account config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._cloud_input: dict[str, Any] = {}
        self._cloud_devices: list[CloudDevice] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_cloud_device(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select one robot when an account contains multiple devices."""
        errors: dict[str, str] = {}
        choices = {
            device.device_id: (f"{device.name} ({_model_label(device.product_id)})")
            for device in self._cloud_devices
        }
        if user_input is not None:
            selected = next(
                (item for item in self._cloud_devices if item.device_id == user_input["device_id"]),
                None,
            )
            if selected is not None:
                return await self._create_cloud_entry(selected)
            errors["base"] = "device_not_found"
        return self.async_show_form(
            step_id="cloud_device",
            data_schema=vol.Schema({vol.Required("device_id"): vol.In(choices)}),
            errors=errors,
        )

    async def _create_cloud_entry(self, device: CloudDevice) -> ConfigFlowResult:
        await self.async_set_unique_id(device.device_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=device.name or f"Narwal {device.product_id}",
            data={
                CONF_PHONE: self._cloud_input[CONF_PHONE].strip(),
                CONF_PASSWORD: self._cloud_input[CONF_PASSWORD],
                CONF_AREA_CODE: self._cloud_input[CONF_AREA_CODE],
                CONF_COUNTRY_CODE: self._cloud_input[CONF_COUNTRY_CODE].upper(),
                "device_id": device.device_id,
                CONF_PRODUCT_KEY: device.product_id,
                CONF_MODEL: _model_label(device.product_id),
            },
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Start cloud reauthentication for an existing entry."""
        self._cloud_input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate replacement cloud credentials and reload the entry."""
        errors: dict[str, str] = {}
        if user_input is not None:
            area_code = _stored_code(self._cloud_input.get(CONF_AREA_CODE), DEFAULT_AREA_CODE)
            country_code = _stored_code(
                self._cloud_input.get(CONF_COUNTRY_CODE),
                DEFAULT_COUNTRY_CODE,
            ).upper()
            updated = {
                **self._cloud_input,
                CONF_PHONE: _normalize_phone(user_input[CONF_PHONE], area_code),
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
                await api.broker()
            except NarwalCloudAuthError:
                errors["base"] = "invalid_auth"
            except NarwalCloudError:
                errors["base"] = "cannot_connect"
            else:
                configured_device_id = self._cloud_input.get("device_id")
                if not any(item.device_id == configured_device_id for item in devices):
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
                        default=str(self._cloud_input.get(CONF_PHONE) or ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


def _stored_code(value: Any, default: str) -> str:
    """Normalize a stored region code without accepting ``None`` as text."""
    normalized = "" if value is None else str(value).strip()
    return normalized or default
