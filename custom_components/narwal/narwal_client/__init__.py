"""Narwal robot vacuum client library — local and cloud transports."""

from .client import (
    NarwalAuthenticationError,
    NarwalClient,
    NarwalCommandError,
    NarwalConnectionError,
)
from .cloud_api import (
    CloudAccount,
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudAuthError,
    NarwalCloudError,
)
from .cloud_client import NarwalCloudClient
from .const import CommandResult, FanLevel, MopHumidity, WorkingStatus
from .models import (
    CommandResponse,
    ConsumableInfo,
    DeviceInfo,
    MapData,
    MapDisplayData,
    NarwalNotification,
    NarwalState,
    RoomInfo,
)
from .protocol import build_frame, parse_frame

__all__ = [
    "NarwalClient",
    "NarwalCloudClient",
    "NarwalCloudApi",
    "NarwalCloudError",
    "NarwalCloudAuthError",
    "CloudAccount",
    "CloudDevice",
    "NarwalCommandError",
    "NarwalConnectionError",
    "NarwalAuthenticationError",
    "NarwalState",
    "NarwalNotification",
    "CommandResponse",
    "CommandResult",
    "ConsumableInfo",
    "DeviceInfo",
    "FanLevel",
    "MapData",
    "MapDisplayData",
    "MopHumidity",
    "RoomInfo",
    "WorkingStatus",
    "build_frame",
    "parse_frame",
]
