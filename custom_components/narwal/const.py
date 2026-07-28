"""Constants for the Narwal vacuum integration."""

from homeassistant.const import Platform

from .narwal_client import FanLevel

DOMAIN = "narwal"
CONF_PHONE = "phone"
CONF_PASSWORD = "password"
CONF_AREA_CODE = "area_code"
CONF_COUNTRY_CODE = "country_code"
DEFAULT_AREA_CODE = "86"
DEFAULT_COUNTRY_CODE = "CN"

MANUFACTURER = "Narwal"

# Product mapping used for cloud device display names.
NARWAL_MODELS: dict[str, str] = {
    "Narwal J3": "cUlfJN5JYP",
    "Narwal J4": "EHf6cRNRGT",
    "Narwal J4 Lite": "6NjIDYxBXb",
    "Narwal J5": "hEA7OEshlx",
    "Narwal Flow": "QoEsI5qYXO",
    "Narwal Flow 2": "QxMSPG6VSO",
    "Narwal Freo Z10 Ultra": "DrzDKQ0MU8",
    "Narwal Freo X10 Pro": "CNbforyZWI",
}

CONF_MODEL = "model"
CONF_PRODUCT_KEY = "product_key"

# GetFeatureList.Response.supportSingleDryMop
FEATURE_SUPPORT_SINGLE_DRY_MOP = 38
FEATURE_SUPPORT_QUIET_DUST_GATHERING = 21

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.EVENT,
]

FAN_SPEED_MAP: dict[str, FanLevel] = {
    "quiet": FanLevel.QUIET,
    "normal": FanLevel.NORMAL,
    "strong": FanLevel.STRONG,
    "max": FanLevel.MAX,
}

FAN_SPEED_LIST: list[str] = list(FAN_SPEED_MAP.keys())
