"""Integration tests for app-confirmed consumable resets and their guards."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.exceptions import HomeAssistantError

from custom_components.narwal.binary_sensor import NarwalConsumableReminderSensor
from custom_components.narwal.button import (
    NarwalConsumableResetButton,
    NarwalResumeBaseSearchButton,
    async_setup_entry,
)
from custom_components.narwal.narwal_client.client import (
    NarwalClient,
    NarwalCommandError,
    NarwalCommandNotSentError,
    NarwalCommandTimeoutError,
    NarwalConnectionError,
)
from custom_components.narwal.narwal_client.cloud_api import (
    CloudAccount,
    CloudDevice,
    NarwalCloudApi,
    NarwalCloudError,
)
from custom_components.narwal.narwal_client.cloud_client import NarwalCloudClient
from custom_components.narwal.narwal_client.const import (
    BASE_STATION_NOT_FOUND_ERROR,
    CommandResult,
)
from custom_components.narwal.narwal_client.error_codes import load_error_catalog
from custom_components.narwal.narwal_client.models import (
    CommandResponse,
    ConsumableInfo,
    NarwalState,
)
from custom_components.narwal.sensor import NarwalConsumableSensor, NarwalRobotErrorSensor
from custom_components.narwal.vacuum import NarwalVacuum


def _success() -> CommandResponse:
    return CommandResponse(result_code=CommandResult.SUCCESS)


def _item(
    type_id: int,
    *,
    category: int = 2,
    reset_supported: bool = True,
    name: str = "Consumable",
) -> ConsumableInfo:
    return ConsumableInfo(
        key=f"type_{type_id}",
        name=name,
        type_id=type_id,
        category=category,
        total_duration=100,
        usage_duration=10,
        reset_supported=reset_supported,
    )


def _coordinator(state: NarwalState) -> SimpleNamespace:
    # Existing entity scenarios start with a successful cloud snapshot.
    state.cloud_consumables_available = True
    return SimpleNamespace(
        config_entry=SimpleNamespace(
            data={"device_id": "test-device", "model": "Narwal Test"}, title="Robot"
        ),
        client=SimpleNamespace(
            state=state,
            robot_awake=True,
            reset_consumable=AsyncMock(return_value=_success()),
            reset_cloud_consumable=AsyncMock(),
        ),
        data=state,
        cloud_metadata_available=True,
        last_update_success=True,
        supports_single_dry_mop=False,
        supports_dust_gathering=False,
        async_add_listener=Mock(return_value=Mock()),
        async_update_listeners=Mock(),
        async_request_refresh=AsyncMock(),
    )


def _reset_buttons(added: list) -> list[NarwalConsumableResetButton]:
    return [entity for entity in added if isinstance(entity, NarwalConsumableResetButton)]


def _entry(coordinator: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(runtime_data=coordinator, async_on_unload=Mock())


async def _setup_buttons(coordinator: SimpleNamespace, added: list) -> None:
    await async_setup_entry(Mock(), _entry(coordinator), lambda entities: added.extend(entities))


class _CloudResponse:
    def __init__(self, body: object) -> None:
        self._body = body

    async def __aenter__(self) -> _CloudResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def raise_for_status(self) -> None:
        return None

    async def json(self, *, content_type: object = None) -> object:
        del content_type
        return self._body


class _CloudSession:
    """Fake aiohttp session recording the outgoing cloud request."""

    def __init__(self, body: object) -> None:
        self._body = body
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _CloudResponse:
        self.calls.append((method, url, kwargs))
        return _CloudResponse(self._body)


def test_robot_error_sensor_localizes_and_lists_multiple_active_errors() -> None:
    """Users see a readable localized fault and unknown codes stay visible."""
    state = NarwalState()
    state.update_from_base_status({"1": [{"1": 0x02100050}, {"1": 0xDEADBEEF}]})
    sensor = NarwalRobotErrorSensor(_coordinator(state), load_error_catalog())
    sensor.hass = SimpleNamespace(config=SimpleNamespace(language="fr"))

    assert sensor.native_value is not None
    assert "station de base" in sensor.native_value.lower()
    errors = sensor.extra_state_attributes["errors"]
    assert [error["code"] for error in errors] == ["0x02100050", "0xDEADBEEF"]
    assert errors[1]["message"] == "0xDEADBEEF"

    state.update_from_base_status({})
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"errors": []}


def test_start_and_resume_button_follow_paused_task_not_error_or_dock() -> None:
    """Start resumes paused tasks; a docked idle robot starts a new clean."""
    state = NarwalState()
    coordinator = _coordinator(state)
    coordinator.client.start = AsyncMock(return_value=_success())
    coordinator.client.resume = AsyncMock(return_value=_success())
    button = NarwalResumeBaseSearchButton(coordinator)
    vacuum = NarwalVacuum(coordinator)

    state.update_from_base_status(
        {"1": {"1": BASE_STATION_NOT_FOUND_ERROR}, "3": {"1": 1}, "11": 2}
    )
    assert not button.available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(button.async_press())
    asyncio.run(vacuum.async_start())
    coordinator.client.start.assert_awaited_once()
    coordinator.client.resume.assert_not_awaited()

    state.update_from_base_status({"1": {"1": 0x02100017}, "3": {"1": 5, "2": 1}, "11": 1})
    assert button.available
    assert vacuum.activity == VacuumActivity.PAUSED
    asyncio.run(vacuum.async_start())
    coordinator.client.resume.assert_awaited_once_with(timeout=10.0)
    coordinator.client.start.assert_awaited_once()

    state.update_from_base_status(
        {
            "1": [{"1": BASE_STATION_NOT_FOUND_ERROR}, {"1": 0x02100017}],
            "3": {"1": 14, "2": 1},
            "11": 2,
        }
    )
    assert button.available
    assert vacuum.activity == VacuumActivity.PAUSED
    asyncio.run(button.async_press())
    assert coordinator.client.resume.await_count == 2
    coordinator.client.start.assert_awaited_once()

    state.update_from_base_status({"3": {"1": 14}, "11": 2})
    assert not button.available


def test_reset_request_routes_to_exact_robot_topic_and_protobuf() -> None:
    """The robot reset route and its protobuf are the App-confirmed contract."""
    client = NarwalClient(device_id="device", product_id="product")
    client.send_command = AsyncMock(return_value=_success())  # type: ignore[method-assign]

    asyncio.run(client.reset_consumable(category="replace", protocol_value=3))
    assert client.send_command.await_args_list == [
        call("consumable/reset_consumable_info", b"\x0a\x02\x10\x03"),
        call("consumable/get_consumable_info"),
    ]

    client.send_command.reset_mock()
    asyncio.run(client.reset_consumable(category="maintain", protocol_value=1))
    assert client.send_command.await_args_list[0] == call(
        "consumable/reset_consumable_info", b"\x0a\x02\x08\x01"
    )


def test_cloud_reset_uses_app_v1_path_and_exact_payload() -> None:
    """The silver-ion reset keeps the App's HTTP path and request body."""
    session = _CloudSession({"code": "0", "result": {}})
    api = NarwalCloudApi(session, phone="phone", password="")
    api.account = CloudAccount(token="token", uuid="user")

    asyncio.run(api.reset_cloud_consumable(device_id="device", product_id="product", type_id=110))

    method, url, kwargs = session.calls[0]
    assert method == "POST"
    assert url.endswith("/consumables-management-app-server/v1/consumables/reset")
    assert kwargs["json"] == {
        "product_id": "product",
        "device_id": "device",
        "type": [110],
        "language": "zh_CN",
    }


def test_cloud_consumables_create_buttons_with_app_reset_routes() -> None:
    """Every declared cloud consumable exposes its App reset route."""
    state = NarwalState(
        consumables={
            item.key: item
            for item in (
                _item(101, category=1, name="尘盒"),
                _item(104, category=1, name="基站清洁组件"),
                _item(3),
                _item(110),
            )
        }
    )
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    coordinator = _coordinator(state)
    added: list = []
    asyncio.run(_setup_buttons(coordinator, added))

    buttons = _reset_buttons(added)
    assert len(buttons) == 4
    assert all(button.available for button in buttons)
    for button in buttons:
        asyncio.run(button.async_press())

    # Buttons are created in the cloud list order.
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
        {"category": "replace", "protocol_value": 3},
    ]
    coordinator.client.reset_cloud_consumable.assert_awaited_once_with(type_id=110)
    assert coordinator.async_request_refresh.await_count == 4

    # The App hides the reset button for this item, so the entity must refuse.
    state.consumables["type_3"] = _item(3, reset_supported=False)
    assert not buttons[2].available
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(buttons[2].async_press())

    coordinator.cloud_metadata_available = False
    assert not any(button.available for button in buttons)


def test_missing_category_does_not_guess_a_cloud_reset_route() -> None:
    """A cloud type id without a category cannot authorize a guessed reset."""
    state = NarwalState(consumables={"type_101": _item(101, category=0)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1}
    coordinator = _coordinator(state)
    added: list = []
    asyncio.run(_setup_buttons(coordinator, added))

    buttons = _reset_buttons(added)
    assert len(buttons) == 1  # Only the independently reported robot maintenance item.
    assert buttons[0].available
    asyncio.run(buttons[0].async_press())
    coordinator.client.reset_consumable.assert_awaited_once_with(
        category="maintain", protocol_value=1
    )


def test_reset_buttons_exist_before_robot_answers_consumable_query() -> None:
    """Entity registration must not depend on the robot being awake at startup."""
    state = NarwalState(consumables={"type_3": _item(3)})
    coordinator = _coordinator(state)  # mqtt_consumables_supported is still False
    added: list = []
    asyncio.run(_setup_buttons(coordinator, added))

    buttons = _reset_buttons(added)
    assert len(buttons) == 1
    assert not buttons[0].available  # Registered, but not yet authorized.

    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    assert buttons[0].available
    asyncio.run(buttons[0].async_press())
    coordinator.client.reset_consumable.assert_awaited_once_with(
        category="replace", protocol_value=3
    )


def test_declared_maintenance_resets_without_an_active_reminder() -> None:
    """A declared maintenance item stays resettable after the reminder clears."""
    state = NarwalState(
        consumables={
            item.key: item
            for item in (
                _item(101, category=1, reset_supported=False),
                _item(104, category=1, reset_supported=False),
            )
        }
    )
    coordinator = _coordinator(state)
    added: list = []
    asyncio.run(_setup_buttons(coordinator, added))

    buttons = _reset_buttons(added)
    assert len(buttons) == 2
    assert not any(button.available for button in buttons)

    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    assert all(button.available for button in buttons)
    for button in buttons:
        asyncio.run(button.async_press())
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
    ]

    state.consumables.clear()
    assert not any(button.available for button in buttons)
    with pytest.raises(HomeAssistantError, match="not currently available"):
        asyncio.run(buttons[0].async_press())


def test_robot_reminder_adds_fallback_and_cloud_declaration_takes_precedence() -> None:
    """A robot reminder backs items the cloud omits; cloud wins when both exist."""
    state = NarwalState()
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1, 4}
    coordinator = _coordinator(state)
    added: list = []
    asyncio.run(_setup_buttons(coordinator, added))

    buttons = _reset_buttons(added)
    assert len(buttons) == 2
    assert all(button.available for button in buttons)
    for button in buttons:
        asyncio.run(button.async_press())
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
    ]

    # The cloud list arrives later and takes over the same reset route.
    state.consumables["type_101"] = _item(101, category=1)
    coordinator.async_add_listener.call_args.args[0]()
    updated = _reset_buttons(added)
    assert len(updated) == 3
    assert sum(button.available for button in updated) == 2
    for button in updated:
        if button.available:
            asyncio.run(button.async_press())
    assert [call.kwargs for call in coordinator.client.reset_consumable.await_args_list] == [
        {"category": "maintain", "protocol_value": 1},
        {"category": "maintain", "protocol_value": 4},
        {"category": "maintain", "protocol_value": 4},
        {"category": "maintain", "protocol_value": 1},
    ]


def test_partial_consumables_failure_disables_cloud_authorized_resets() -> None:
    """A stale consumable list must not authorize resets after a partial outage."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice(
            device_id="device",
            product_id="product",
            name="Robot",
            firmware_version="1",
            serial_number="serial",
        )
        items = [
            _item(3),
            _item(110),
            _item(101, category=1),
            _item(104, category=1, reset_supported=False),
        ]
        client._api = SimpleNamespace(
            consumables=AsyncMock(side_effect=[items, NarwalCloudError("unavailable"), items, []]),
            notifications=AsyncMock(side_effect=[[], ["new notification"], [], []]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        added: list = []
        entry = _entry(coordinator)

        await client.get_cloud_metadata()
        lifetime = NarwalConsumableSensor(coordinator, state.consumables["type_3"])
        reminder_sensor = NarwalConsumableReminderSensor(coordinator)
        assert lifetime.available and reminder_sensor.available
        await async_setup_entry(Mock(), entry, lambda entities: added.extend(entities))
        buttons = _reset_buttons(added)
        assert len(buttons) == 4
        assert all(button.available for button in buttons)

        await client.get_cloud_metadata()  # Other metadata still succeeds.
        coordinator.async_add_listener.call_args.args[0]()
        assert not lifetime.available and not reminder_sensor.available
        independent = [button for button in _reset_buttons(added) if button not in buttons]
        assert len(independent) == 1 and independent[0].available
        for button in buttons:
            assert not button.available
            with pytest.raises(HomeAssistantError, match="not currently available"):
                await button.async_press()
        coordinator.client.reset_consumable.assert_not_awaited()
        coordinator.client.reset_cloud_consumable.assert_not_awaited()
        await independent[0].async_press()
        coordinator.client.reset_consumable.assert_awaited_once_with(
            category="maintain", protocol_value=1
        )

        await client.get_cloud_metadata()  # Recovery.
        assert lifetime.available and reminder_sensor.available
        assert all(button.available for button in buttons)
        assert not independent[0].available
        for button in buttons:
            await button.async_press()
        coordinator.client.reset_cloud_consumable.assert_awaited_once_with(type_id=110)
        assert coordinator.client.reset_consumable.await_count == 4

        await client.get_cloud_metadata()  # A valid empty list really withdraws them.
        assert not state.consumables
        assert not lifetime.available and not reminder_sensor.available
        assert all(not button.available for button in buttons)
        assert independent[0].available

    asyncio.run(run())


def test_malformed_cloud_consumables_keep_cache_but_disable_entities() -> None:
    """A damaged external response must not look like a successful empty list."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        api = NarwalCloudApi(Mock(), phone="phone", password="password")
        api.account = CloudAccount(token="token", uuid="user")
        api._request = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "consumable_list": [
                        {
                            "type": 3,
                            "item_type": 2,
                            "name": "Brush",
                            "total_duration": 100,
                            "usage_duration": 20,
                            "reset_btn_switch": 1,
                        }
                    ]
                },
                {"consumable_list": "invalid"},
                {"consumable_list": [None]},
                {
                    "consumable_list": [
                        {"type": 3, "total_duration": 100, "usage_duration": "invalid"}
                    ]
                },
                {"consumable_list": []},
            ]
        )
        api.notifications = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.task_records = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health_current = AsyncMock(return_value={})  # type: ignore[method-assign]
        client._api = api
        coordinator = _coordinator(client.state)
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        await client.get_cloud_metadata()
        item = state.consumables["type_3"]
        lifetime = NarwalConsumableSensor(coordinator, item)
        added: list = []
        await _setup_buttons(coordinator, added)
        button = _reset_buttons(added)[0]
        assert lifetime.available and lifetime.native_value == 80 and button.available

        for _ in range(3):
            await client.get_cloud_metadata()  # Other metadata endpoints still succeed.
            assert state.consumables["type_3"] is item
            assert not lifetime.available and not button.available
            with pytest.raises(HomeAssistantError, match="not currently available"):
                await button.async_press()
        coordinator.client.reset_consumable.assert_not_awaited()
        await client.get_cloud_metadata()  # A valid empty list really removes the item.
        assert not state.consumables and not lifetime.available and not button.available

    asyncio.run(run())


def test_missing_usage_duration_keeps_freshly_reset_consumable_usable() -> None:
    """The cloud omits a zero usage duration right after replacement/reset."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        api = NarwalCloudApi(Mock(), phone="phone", password="password")
        api.account = CloudAccount(token="token", uuid="user")
        missing: dict[str, object] = {
            "type": 3,
            "item_type": 2,
            "name": "Brush",
            "total_duration": 300,
        }
        api._request = AsyncMock(  # type: ignore[method-assign]
            return_value={"consumable_list": [missing, {**missing, "usage_duration": None}]}
        )
        api.notifications = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.task_records = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health = AsyncMock(return_value=[])  # type: ignore[method-assign]
        api.dust_bag_health_current = AsyncMock(return_value={})  # type: ignore[method-assign]
        client._api = api
        coordinator = _coordinator(client.state)
        await client.get_cloud_metadata()

        assert client.state.cloud_consumables_available
        item = client.state.consumables["type_3"]
        lifetime = NarwalConsumableSensor(coordinator, item)
        assert lifetime.available and lifetime.native_value == 100

    asyncio.run(run())


def test_mqtt_disconnect_invalidates_reminder_and_ignores_delayed_reply() -> None:
    """Disconnect cannot leave a pre-disconnect reminder marked as current."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        state = client.state
        state.consumables = {"type_3": _item(3)}
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        reminder = NarwalConsumableReminderSensor(coordinator)
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_maintain_1",
            category="maintain",
            protocol_value=1,
            translation_key="reset_maintain_dust_bin",
        )
        assert button.available
        started, release = asyncio.Event(), asyncio.Event()
        reads = 0

        async def send(*args: object, **kwargs: object) -> CommandResponse:
            nonlocal reads
            reads += 1
            if reads == 1:
                started.set()
                await release.wait()
            return CommandResponse(data={"1": {"1": [1]}})

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        old_read = asyncio.create_task(client.get_consumable_info())
        await started.wait()
        await client.disconnect()
        assert not button.available and reminder.is_on is None
        release.set()
        await old_read
        assert not button.available and reminder.is_on is None
        await client.get_consumable_info()
        assert button.available

    asyncio.run(run())


def test_confirmed_reset_keeps_button_disabled_until_its_read_returns() -> None:
    """The window between the ACK and the confirming read must not allow a second reset."""

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        state.consumables = {"type_101": _item(101, category=1)}
        coordinator = _coordinator(state)
        coordinator.client.reset_consumable = client.reset_consumable
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_type_101",
            category="maintain",
            protocol_value=1,
            translation_key="reset_consumable",
            consumable_key="type_101",
            consumable_type_id=101,
        )
        assert button.available
        read_started, release_read = asyncio.Event(), asyncio.Event()

        async def send(topic: str, *args: object, **kwargs: object) -> CommandResponse:
            if topic == "consumable/get_consumable_info":
                read_started.set()
                await release_read.wait()
                return CommandResponse(data={"1": {"1": []}})
            return _success()

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        press = asyncio.create_task(button.async_press())
        await read_started.wait()
        assert not button.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        release_read.set()
        await press
        assert client.send_command.await_count == 2
        assert not state.consumable_maintain_items

    asyncio.run(run())


def test_reset_that_was_never_published_can_be_retried() -> None:
    """A transport failure before publishing did not reset anything."""
    disconnected = NarwalCloudClient(
        Mock(), phone="phone", password="password", device_id="device", product_id="product"
    )
    assert not disconnected.connected
    for _ in range(2):
        # The second attempt only reaches the transport when the first one is
        # not mistaken for an unknown outcome.
        with pytest.raises(NarwalConnectionError):
            asyncio.run(disconnected.reset_consumable(category="maintain", protocol_value=1))

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        coordinator = _coordinator(state)
        coordinator.client.reset_consumable = client.reset_consumable
        client.send_command = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                NarwalCommandNotSentError("MQTT response subscription failed"),
                CommandResponse(data={"1": {"1": [1]}}),
                _success(),
                CommandResponse(data={"1": {"1": []}}),
            ]
        )
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_maintain_1",
            category="maintain",
            protocol_value=1,
            translation_key="reset_maintain_dust_bin",
        )
        assert button.available
        with pytest.raises(HomeAssistantError, match="connection is unavailable"):
            await button.async_press()
        # The next status poll confirms the reminder, so the user may press again.
        await client.get_consumable_info()
        assert button.available
        await button.async_press()
        assert client.send_command.await_count == 4
        assert not state.consumable_maintain_items

    asyncio.run(run())


def test_inflight_robot_read_cannot_overwrite_reset_or_allow_parallel_reset() -> None:
    """A delayed pre-reset reply cannot restore an old robot reminder."""

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        read_started, release_read = asyncio.Event(), asyncio.Event()
        reset_started, release_reset = asyncio.Event(), asyncio.Event()
        reads = 0

        async def send(topic: str, *args: object, **kwargs: object) -> CommandResponse:
            nonlocal reads
            if topic == "consumable/get_consumable_info":
                reads += 1
                if reads == 1:
                    read_started.set()
                    await release_read.wait()
                    return CommandResponse(data={"1": {"1": [1]}})
                return CommandResponse(data={"1": {"1": []}})
            reset_started.set()
            await release_reset.wait()
            return _success()

        client.send_command = AsyncMock(side_effect=send)  # type: ignore[method-assign]
        old_read = asyncio.create_task(client.get_consumable_info())
        await read_started.wait()
        reset = asyncio.create_task(client.reset_consumable(category="maintain", protocol_value=1))
        await reset_started.wait()
        with pytest.raises(NarwalCommandError):
            await client.reset_consumable(category="maintain", protocol_value=1)
        release_reset.set()
        await reset
        assert not state.mqtt_consumables_available and not state.consumable_maintain_items
        release_read.set()
        await old_read
        assert not state.mqtt_consumables_available and not state.consumable_maintain_items

    asyncio.run(run())


def test_inflight_cloud_read_cannot_overwrite_reset_result() -> None:
    """The latest post-reset cloud snapshot wins over an older delayed read."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        old = _item(110)
        new = _item(110, reset_supported=False)
        read_started, release_read = asyncio.Event(), asyncio.Event()
        reads = 0

        async def read(**kwargs: object) -> list[ConsumableInfo]:
            nonlocal reads
            reads += 1
            if reads == 1:
                read_started.set()
                await release_read.wait()
                return [old]
            return [new]

        reset_started, release_reset = asyncio.Event(), asyncio.Event()

        async def reset_request(**kwargs: object) -> None:
            reset_started.set()
            await release_reset.wait()

        client._api = SimpleNamespace(
            consumables=AsyncMock(side_effect=read),
            reset_cloud_consumable=AsyncMock(side_effect=reset_request),
            notifications=AsyncMock(return_value=[]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        old_read = asyncio.create_task(client.get_cloud_metadata())
        await read_started.wait()
        reset = asyncio.create_task(client.reset_cloud_consumable(type_id=110))
        await reset_started.wait()
        with pytest.raises(NarwalConnectionError):
            await client.reset_cloud_consumable(type_id=110)
        release_reset.set()
        await reset
        client._api.reset_cloud_consumable.assert_awaited_once()
        await client.get_cloud_metadata()
        assert client.state.consumables[old.key] is new
        release_read.set()
        await old_read
        assert client.state.cloud_consumables_available
        assert client.state.consumables[old.key] is new

    asyncio.run(run())


def test_successful_robot_reset_is_not_reported_failed_when_followup_query_times_out() -> None:
    """A confirmed write must not invite a retry merely because its read failed."""
    client = NarwalClient(device_id="device", product_id="product")
    state = client.state
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    state.consumable_maintain_items = {1}
    coordinator = _coordinator(state)
    coordinator.cloud_metadata_available = False
    coordinator.client.reset_consumable = client.reset_consumable
    client.send_command = AsyncMock(  # type: ignore[method-assign]
        side_effect=[
            _success(),
            NarwalCommandTimeoutError(),
        ]
    )
    button = NarwalConsumableResetButton(
        coordinator,
        key="reset_consumable_maintain_1",
        category="maintain",
        protocol_value=1,
        translation_key="reset_maintain_dust_bin",
    )
    assert button.available
    coordinator.async_request_refresh.side_effect = RuntimeError("refresh failed")
    asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_awaited_once()
    assert not button.available
    assert client.send_command.await_count == 2


def test_ambiguous_cloud_reset_disables_cached_entities_until_refreshed() -> None:
    """A cloud reset timeout might mean the reset was already applied."""

    async def run() -> None:
        client = NarwalCloudClient(
            Mock(), phone="phone", password="password", device_id="device", product_id="product"
        )
        client.cloud_device = CloudDevice("device", "product", "Robot", "1", "serial")
        item = _item(110)
        state = client.state
        state.consumables = {item.key: item}
        coordinator = _coordinator(state)
        coordinator.client.reset_cloud_consumable = client.reset_cloud_consumable
        client._api = SimpleNamespace(
            reset_cloud_consumable=AsyncMock(side_effect=NarwalCloudError("timeout")),
            consumables=AsyncMock(return_value=[item]),
            notifications=AsyncMock(return_value=[]),
            task_records=AsyncMock(return_value=[]),
            dust_bag_health=AsyncMock(return_value=[]),
            dust_bag_health_current=AsyncMock(return_value={}),
        )
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_type_110",
            category="cloud",
            protocol_value=110,
            translation_key="reset_consumable",
            consumable_key=item.key,
            consumable_type_id=110,
        )
        lifetime = NarwalConsumableSensor(coordinator, item)
        assert button.available and lifetime.available
        with pytest.raises(HomeAssistantError, match="cloud consumable reset failed"):
            await button.async_press()
        assert not button.available and not lifetime.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        client._api.reset_cloud_consumable.assert_awaited_once()
        await client.get_cloud_metadata()
        assert not button.available and lifetime.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()
        client._api.reset_cloud_consumable.assert_awaited_once()

    asyncio.run(run())


def test_ambiguous_robot_reset_disables_item_for_the_session() -> None:
    """A timed-out robot reset might already be applied, so no retry is authorized."""

    async def run() -> None:
        client = NarwalClient(device_id="device", product_id="product")
        state = client.state
        state.mqtt_consumables_supported = state.mqtt_consumables_available = True
        state.consumable_maintain_items = {1}
        state.consumables = {"type_101": _item(101, category=1)}
        coordinator = _coordinator(state)
        coordinator.client.reset_consumable = client.reset_consumable
        button = NarwalConsumableResetButton(
            coordinator,
            key="reset_consumable_type_101",
            category="maintain",
            protocol_value=1,
            translation_key="reset_consumable",
            consumable_key="type_101",
            consumable_type_id=101,
        )
        assert button.available
        client.send_command = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                NarwalCommandTimeoutError("timeout"),
                CommandResponse(data={"1": {"1": [1]}}),
            ]
        )
        with pytest.raises(HomeAssistantError, match="timed out"):
            await button.async_press()
        assert not button.available

        # A later successful read does not lift the unknown-outcome guard.
        await client.get_consumable_info()
        assert not button.available
        with pytest.raises(HomeAssistantError, match="not currently available"):
            await button.async_press()

    asyncio.run(run())


def test_failed_robot_reset_does_not_refresh() -> None:
    state = NarwalState(consumables={"type_3": _item(3)})
    state.mqtt_consumables_supported = state.mqtt_consumables_available = True
    coordinator = _coordinator(state)
    coordinator.client.reset_consumable.return_value = CommandResponse(result_code=2)
    button = NarwalConsumableResetButton(
        coordinator,
        key="reset_consumable_type_3",
        category="replace",
        protocol_value=3,
        translation_key="reset_consumable",
        consumable_key="type_3",
        consumable_type_id=3,
    )

    with pytest.raises(HomeAssistantError):
        asyncio.run(button.async_press())
    coordinator.async_request_refresh.assert_not_awaited()
