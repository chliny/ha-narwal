"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import FanLevel

DOMAIN = "narwal"
DEFAULT_PORT = 9002
CONNECTION_LOCAL = "local"
CONNECTION_CLOUD = "cloud"
CONF_CONNECTION_TYPE = "connection_type"
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_AREA_CODE = "area_code"
CONF_COUNTRY_CODE = "country_code"
DEFAULT_AREA_CODE = "86"
DEFAULT_COUNTRY_CODE = "CN"

MANUFACTURER = "Narwal"

# Current product mapping used by cloud display and LAN selection.
# Keys are user-facing labels; values are product key prefixes.
# "auto" cycles all known keys during discovery (slower, fallback).
NARWAL_MODELS: dict[str, str] = {
    "Narwal J3": "cUlfJN5JYP",
    "Narwal J4": "EHf6cRNRGT",
    "Narwal J4 Lite": "6NjIDYxBXb",
    "Narwal J5": "hEA7OEshlx",
    "Narwal Flow": "QoEsI5qYXO",
    "Narwal Flow 2": "QxMSPG6VSO",
    "Narwal Freo Z10 Ultra": "DrzDKQ0MU8",
    "Narwal Freo X10 Pro": "CNbforyZWI",
    "Other / Auto-detect": "auto",
}

CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SELECT,
]

FAN_SPEED_MAP: dict[str, FanLevel] = {
    "quiet": FanLevel.QUIET,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.MAX,
}

FAN_SPEED_LIST: list[str] = list(FAN_SPEED_MAP.keys())
