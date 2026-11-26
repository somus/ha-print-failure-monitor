"""The integration end to end: checks, alerting, auto-pause, notification actions, snooze, errors."""

import asyncio
import io
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.camera import Image
from homeassistant.components.persistent_notification import _async_get_or_create_notifications
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from PIL import Image as PILImage
from pytest_homeassistant_custom_component.common import async_capture_events, async_mock_service
from pytest_homeassistant_custom_component.components.diagnostics import get_diagnostics_for_config_entry

from custom_components.print_failure_monitor.api import AuthError
from custom_components.print_failure_monitor.const import ACTION_PREFIX, CONF_AUTO_PAUSE, CONF_ENABLED
from custom_components.print_failure_monitor.history import FrameHistory

COORDINATOR = "custom_components.print_failure_monitor.coordinator"
FAILED = {"plate_visible": 0.9, "spaghetti": 0.92, "detached": 0.2, "nozzle_blob": 0.1}
CLEAN = {"plate_visible": 0.9, "spaghetti": 0.05, "detached": 0.1, "nozzle_blob": 0.1}
DARK = {"plate_visible": 0.2, "spaghetti": 0.9, "detached": 0.9, "nozzle_blob": 0.9}


@pytest.fixture
def ask():
    with patch(f"{COORDINATOR}.ask", new_callable=AsyncMock) as mock:
        mock.return_value = (CLEAN, {"cost": 0.0002})
        yield mock


@pytest.fixture(autouse=True)
def camera(jpeg):
    with patch(f"{COORDINATOR}.async_get_image", AsyncMock(return_value=Image("image/jpeg", jpeg))):
        yield


@pytest.fixture
def notifications(hass: HomeAssistant):
    return async_mock_service(hass, "notify", "mobile_app_phone")


@pytest.fixture(autouse=True)
def _config_dir(config_dir):
    return config_dir


async def _setup(hass, make_entry, status="running", data=None, **options):
    hass.states.async_set("sensor.p1s_print_status", status)
    for button in ("pause", "resume", "stop"):
        hass.states.async_set(f"button.p1s_{button}", "unknown")  # a button that was never pressed
    entry = make_entry({"notify_services": ["notify.mobile_app_phone"], **options}, data)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)  # the first check runs in the background
    assert entry.state is ConfigEntryState.LOADED
    return entry, entry.runtime_data


def _mock_presses(hass):
    """Replace button.press after setup; the integration's own button platform registers the real one."""
    return async_mock_service(hass, "button", "press")


async def _checks(hass, coordinator, n):
    for _ in range(n):
        await coordinator.async_refresh()
    await hass.async_block_till_done()


async def test_entities_created(hass, make_entry, ask):
    await _setup(hass, make_entry)
    for entity_id in (
        "binary_sensor.p1s_print_failure",
        "sensor.p1s_failure_probability",
        "sensor.p1s_last_check",
        "image.p1s_last_checked_frame",
        "switch.p1s_monitoring",
        "switch.p1s_auto_pause",
        "button.p1s_check_now",
    ):
        assert hass.states.get(entity_id) is not None, entity_id
    assert hass.states.get("sensor.p1s_last_check").state == "clean"
    assert hass.states.get("sensor.p1s_failure_probability").state == "10.0"


async def test_idle_when_not_printing(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry, status="idle")
    await _checks(hass, coordinator, 2)
    ask.assert_not_called()
    assert hass.states.get("sensor.p1s_last_check").state == "idle"


async def test_alert_without_auto_pause(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)  # first check happens during setup
    presses = _mock_presses(hass)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"
    await _checks(hass, coordinator, 2)

    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    assert presses == []
    assert len(notifications) == 1
    data = notifications[0].data
    assert "spaghetti 92%" in data["message"] and "still running" in data["message"]
    assert [a["title"] for a in data["data"]["actions"]] == ["Pause", "Stop", "Continue"]
    assert data["data"]["image"].startswith("/api/print_failure_monitor/frame/")
    assert "?" not in data["data"]["image"]  # the iOS app escapes a query string into the path

    await _checks(hass, coordinator, 3)
    assert len(notifications) == 1  # one alert per failure, not one per frame


async def test_auto_pause_then_resume(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _entry, coordinator = await _setup(hass, make_entry, auto_pause=True)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)

    assert [c.data["entity_id"] for c in presses] == ["button.p1s_pause"]
    data = notifications[0].data
    assert "was paused" in data["message"]
    actions = {a["title"]: a["action"] for a in data["data"]["actions"]}
    assert list(actions) == ["Resume", "Stop"]

    hass.bus.async_fire("mobile_app_notification_action", {"action": actions["Resume"]})
    await hass.async_block_till_done()
    assert presses[-1].data["entity_id"] == "button.p1s_resume"
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"
    assert hass.states.get("sensor.p1s_last_check").state == "snoozed"

    calls = ask.call_count
    await _checks(hass, coordinator, 3)
    assert ask.call_count == calls  # snoozed: no checks


async def test_notification_pause_and_stop(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _entry, coordinator = await _setup(hass, make_entry)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    actions = {a["title"]: a["action"] for a in notifications[0].data["data"]["actions"]}

    hass.bus.async_fire("mobile_app_notification_action", {"action": actions["Pause"]})
    await hass.async_block_till_done()
    hass.bus.async_fire("mobile_app_notification_action", {"action": actions["Stop"]})
    await hass.async_block_till_done()
    assert [c.data["entity_id"] for c in presses] == ["button.p1s_pause", "button.p1s_stop"]


async def test_continue_without_pause_does_not_resume(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    actions = {a["title"]: a["action"] for a in notifications[0].data["data"]["actions"]}

    hass.bus.async_fire("mobile_app_notification_action", {"action": actions["Continue"]})
    await hass.async_block_till_done()
    assert presses == []  # the printer was never paused
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"


async def test_other_entries_actions_ignored(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"{ACTION_PREFIX}someotherentry_STOP"})
    await hass.async_block_till_done()
    assert presses == []
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"


async def test_unreadable_and_failed_checks_never_count_as_clean(hass, make_entry, ask, notifications):
    ask.return_value = (DARK, {})
    _, coordinator = await _setup(hass, make_entry)
    assert hass.states.get("sensor.p1s_last_check").state == "not_visible"

    ask.side_effect = RuntimeError("HTTP 402: no credits")
    await _checks(hass, coordinator, 1)
    state = hass.states.get("sensor.p1s_last_check")
    assert state.state == "error"
    assert "HTTP 402" in state.attributes["last_error"]
    assert state.attributes["recent_verdicts"] == []

    ask.side_effect = None
    ask.return_value = (FAILED, {})
    await _checks(hass, coordinator, 3)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"


async def test_print_end_resets(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    hass.states.async_set("sensor.p1s_print_status", "finish")
    await _checks(hass, coordinator, 1)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"


async def test_switches_write_options(hass, make_entry, ask):
    entry, coordinator = await _setup(hass, make_entry)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.p1s_auto_pause"}, blocking=True)
    assert entry.options[CONF_AUTO_PAUSE] is True
    assert hass.states.get("switch.p1s_auto_pause").state == "on"

    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.p1s_monitoring"}, blocking=True)
    assert entry.options[CONF_ENABLED] is False
    calls = ask.call_count
    await _checks(hass, coordinator, 2)
    assert ask.call_count == calls
    assert hass.states.get("sensor.p1s_last_check").state == "idle"


async def test_check_now_between_prints(hass, make_entry, ask):
    ask.return_value = (FAILED, {})
    _, _coordinator = await _setup(hass, make_entry, status="idle")
    await hass.services.async_call("button", "press", {"entity_id": "button.p1s_check_now"}, blocking=True)
    await hass.async_block_till_done()
    assert ask.call_count == 1
    assert hass.states.get("sensor.p1s_last_check").state == "failed"
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"  # not counted outside a print


async def test_last_frame_image(hass, make_entry, ask, hass_client, jpeg):
    await _setup(hass, make_entry)
    state = hass.states.get("image.p1s_last_checked_frame")
    client = await hass_client()
    resp = await client.get(state.attributes["entity_picture"])
    assert resp.status == 200
    assert await resp.read() == jpeg


async def test_unload(hass, make_entry, ask):
    entry, _ = await _setup(hass, make_entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_notification_carries_the_triggering_frame(
    hass, make_entry, ask, notifications, hass_client, hass_client_no_auth
):
    """The phone gets the exact frame the model judged by plain path (the app logs in); the browser a signed link."""
    events = async_capture_events(hass, "print_failure_monitor_failure")
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    image = notifications[0].data["data"]["image"]
    trigger = [r for r in coordinator.history.records if r["kind"] == "check"][-1]
    frame = (coordinator.history.dir / f"{trigger['id']}.jpg").read_bytes()
    assert image == f"/api/print_failure_monitor/frame/{entry.entry_id}/{trigger['id']}"

    resp = await (await hass_client()).get(image)  # the companion app sends its login
    assert resp.status == 200 and await resp.read() == frame
    assert (await (await hass_client_no_auth()).get(image)).status == 401

    signed = events[0].data["frame_url"]
    assert signed.startswith(f"{image}?authSig=") and events[0].data["frame_path"] == image
    resp = await (await hass_client_no_auth()).get(signed)
    assert resp.status == 200 and await resp.read() == frame

    notification = _async_get_or_create_notifications(hass)[f"print_failure_monitor_{entry.entry_id}"]
    target = f"/print-failure-monitor?entry={entry.entry_id}&frame={trigger['id']}"
    assert notifications[0].data["data"]["url"] == target  # iOS
    assert notifications[0].data["data"]["clickAction"] == target  # Android
    assert f"]({signed})" in notification["message"]  # a browser <img> sends no login
    assert f"]({target})" in notification["message"]


async def test_notification_falls_back_to_live_camera(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry, save_frames="off")
    await _checks(hass, coordinator, 2)
    data = notifications[0].data["data"]
    assert data["image"] == "/api/camera_proxy/camera.p1s_camera"
    assert data["url"] == data["clickAction"] == f"/print-failure-monitor?entry={entry.entry_id}"
    # The camera proxy needs a login a browser <img> doesn't send, so the persistent one has no picture.
    notification = _async_get_or_create_notifications(hass)[f"print_failure_monitor_{entry.entry_id}"]
    assert "![" not in notification["message"]


async def test_failure_event(hass, make_entry, ask, notifications):
    """Automations get one event per failure with the scores and the same frame link as the notification."""
    ask.return_value = (FAILED, {})
    events = async_capture_events(hass, "print_failure_monitor_failure")
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 4)

    assert len(events) == 1
    data = events[0].data
    trigger = [r for r in coordinator.history.records if r["kind"] == "check"][2]
    assert data["entry_id"] == entry.entry_id and data["printer"] == "P1S"
    assert data["camera"] == "camera.p1s_camera" and data["paused"] is False
    assert data["answers"] == FAILED and data["failures"] == {"spaghetti": 0.92}
    assert data["frame_id"] == trigger["id"]
    assert data["frame_path"] == notifications[0].data["data"]["image"]
    assert data["frame_url"].startswith(data["frame_path"] + "?authSig=")
    assert data["panel_url"] == f"/print-failure-monitor?entry={entry.entry_id}&frame={trigger['id']}"


async def test_check_action_returns_result(hass, make_entry, ask):
    entry, _coordinator = await _setup(hass, make_entry, status="idle")
    ask.return_value = (FAILED, {})
    response = await hass.services.async_call(
        "print_failure_monitor", "check", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    assert response == {"result": "failed", "answers": FAILED, "error": None}
    assert hass.states.get("sensor.p1s_last_check").state == "failed"
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"  # not printing: not counted


async def test_check_action_on_another_camera_leaves_printer_alone(hass, make_entry, ask):
    entry, coordinator = await _setup(hass, make_entry)
    checks = len(coordinator.history.records)
    ask.return_value = (FAILED, {})
    response = await hass.services.async_call(
        "print_failure_monitor",
        "check",
        {"config_entry_id": entry.entry_id, "camera": "camera.other"},
        blocking=True,
        return_response=True,
    )
    assert response["result"] == "failed"
    assert hass.states.get("sensor.p1s_last_check").state == "clean"
    assert len(coordinator.history.records) == checks


async def test_snooze_action(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"

    await hass.services.async_call(
        "print_failure_monitor", "snooze", {"config_entry_id": entry.entry_id, "minutes": 30}, blocking=True
    )
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"
    assert hass.states.get("sensor.p1s_last_check").state == "snoozed"
    calls = ask.call_count
    await _checks(hass, coordinator, 3)
    assert ask.call_count == calls

    await hass.services.async_call(
        "print_failure_monitor", "snooze", {"config_entry_id": entry.entry_id, "minutes": 0}, blocking=True
    )
    await hass.async_block_till_done()
    assert ask.call_count == calls + 1  # checks again right away
    assert hass.states.get("sensor.p1s_last_check").state == "failed"


async def test_actions_reject_unknown_printer(hass, make_entry, ask):
    await _setup(hass, make_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("print_failure_monitor", "snooze", {"config_entry_id": "nope"}, blocking=True)


async def test_check_action_reports_errors(hass, make_entry, ask):
    entry, _ = await _setup(hass, make_entry)
    ask.side_effect = RuntimeError("HTTP 429")
    response = await hass.services.async_call(
        "print_failure_monitor", "check", {"config_entry_id": entry.entry_id}, blocking=True, return_response=True
    )
    assert response == {"result": "error", "answers": {}, "error": "RuntimeError: HTTP 429"}


# --- print state: pauses and status drops ---------------------------------------------------------

STATUS = "sensor.p1s_print_status"


def _action(notification, title):
    return next(a["action"] for a in notification.data["data"]["actions"] if a["title"] == title)


async def _tap(hass, action):
    hass.bus.async_fire("mobile_app_notification_action", {"action": action})
    await hass.async_block_till_done()


async def test_auto_pause_keeps_alert_and_resume_snooze_through_the_pause(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry, auto_pause=True)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "pause")  # the printer reports the pause
    await _checks(hass, coordinator, 1)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    assert hass.states.get("sensor.p1s_last_check").state == "paused"

    await _tap(hass, _action(notifications[0], "Resume"))
    await _checks(hass, coordinator, 1)  # a tick before the printer reports running again
    hass.states.async_set(STATUS, "running")
    calls = ask.call_count
    await _checks(hass, coordinator, 3)

    assert ask.call_count == calls  # still snoozed
    assert [p.data["entity_id"] for p in presses] == ["button.p1s_pause", "button.p1s_resume"]
    assert len({r["print_id"] for r in coordinator.history.records if r["kind"] == "check"}) == 1


async def test_snooze_action_while_paused(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry, auto_pause=True)
    _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "pause")
    await _checks(hass, coordinator, 1)
    await hass.services.async_call(
        "print_failure_monitor", "snooze", {"config_entry_id": entry.entry_id, "minutes": 60}, blocking=True
    )
    await _checks(hass, coordinator, 1)
    hass.states.async_set(STATUS, "running")
    calls = ask.call_count
    await _checks(hass, coordinator, 3)
    assert ask.call_count == calls
    assert len([n for n in notifications if n.data["message"] != "clear_notification"]) == 1


async def test_short_status_drop_keeps_the_print(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "unavailable")
    await _checks(hass, coordinator, 1)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    hass.states.async_set(STATUS, "running")
    await _checks(hass, coordinator, 3)
    assert len(notifications) == 1  # not alerted again


async def test_long_status_drop_ends_the_print(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "unavailable")
    await _checks(hass, coordinator, 1)
    coordinator._unknown_since -= timedelta(minutes=11)
    await _checks(hass, coordinator, 1)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"
    assert hass.states.get("sensor.p1s_last_check").state == "idle"


async def test_print_end_clears_notifications_and_old_buttons(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    stop = _action(notifications[0], "Stop")

    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert f"print_failure_monitor_{entry.entry_id}" not in _async_get_or_create_notifications(hass)
    clear = {"message": "clear_notification", "data": {"tag": f"print_failure_monitor_{entry.entry_id}"}}
    assert clear in [c.data for c in notifications]

    ask.return_value = (CLEAN, {})
    hass.states.async_set(STATUS, "running")  # the next print is fine
    await _checks(hass, coordinator, 2)
    await _tap(hass, stop)  # a button on the old notification
    assert presses == []


async def test_only_mobile_app_notifications_are_cleared(hass, make_entry, ask):
    other = async_mock_service(hass, "notify", "telegram")
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry, notify_services=["notify.telegram"])
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert [c.data["message"] for c in other if c.data["message"] == "clear_notification"] == []


# --- pressing printer buttons ---------------------------------------------------------------------


@pytest.mark.parametrize("state", [None, "unavailable"])
async def test_pause_button_missing_or_unavailable(hass, make_entry, ask, notifications, state):
    events = async_capture_events(hass, "print_failure_monitor_failure")
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry, auto_pause=True)
    presses = _mock_presses(hass)
    if state is None:
        hass.states.async_remove("button.p1s_pause")
    else:
        hass.states.async_set("button.p1s_pause", state)
    await _checks(hass, coordinator, 2)

    assert presses == []
    assert "still running" in notifications[0].data["message"]
    assert events[0].data["paused"] is False
    assert "Pause" in [a["title"] for a in notifications[0].data["data"]["actions"]]


# --- snooze and Check now -------------------------------------------------------------------------


async def test_check_now_does_not_count_while_snoozed(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    await hass.services.async_call(
        "print_failure_monitor", "snooze", {"config_entry_id": entry.entry_id, "minutes": 60}, blocking=True
    )
    for _ in range(3):
        await coordinator.async_check_now()
    await hass.async_block_till_done()
    assert len([n for n in notifications if n.data["message"] != "clear_notification"]) == 1


async def test_expired_snooze_not_shown_after_pause(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    await hass.services.async_call(
        "print_failure_monitor", "snooze", {"config_entry_id": entry.entry_id, "minutes": 1}, blocking=True
    )
    coordinator._snoozed_until -= timedelta(minutes=5)  # the snooze ran out
    await _checks(hass, coordinator, 3)
    alerts = [n for n in notifications if n.data["message"] != "clear_notification"]
    await _tap(hass, _action(alerts[-1], "Pause"))
    assert hass.states.get("sensor.p1s_last_check").state == "failed"


# --- checks racing other things -------------------------------------------------------------------


async def test_check_in_flight_during_unload_does_nothing(hass, make_entry, notifications):
    gate = asyncio.Event()
    answers = [CLEAN]

    async def slow(*args, **kwargs):
        if answers:
            return answers.pop(), {}
        await gate.wait()
        return FAILED, {}

    with patch(f"{COORDINATOR}.ask", side_effect=slow):
        entry, coordinator = await _setup(hass, make_entry, auto_pause=True, required=1, window=1)
        presses = _mock_presses(hass)
        task = hass.async_create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert await hass.config_entries.async_remove(entry.entry_id)
        gate.set()
        await task
        await hass.async_block_till_done()
    assert presses == [] and notifications == []
    assert _async_get_or_create_notifications(hass) == {}


async def test_check_in_flight_during_snooze_does_not_count(hass, make_entry, notifications):
    gate = asyncio.Event()
    answers = [CLEAN]

    async def slow(*args, **kwargs):
        if answers:
            return answers.pop(), {}
        await gate.wait()
        return FAILED, {}

    with patch(f"{COORDINATOR}.ask", side_effect=slow):
        _entry, coordinator = await _setup(hass, make_entry, required=1, window=1)
        task = hass.async_create_task(coordinator.async_check_now())
        await asyncio.sleep(0)
        await coordinator.async_snooze(30)
        gate.set()
        await task
        await hass.async_block_till_done()
    assert notifications == []


async def test_overlapping_checks_alert_with_their_own_frame(hass, make_entry, ask, notifications):
    _, coordinator = await _setup(hass, make_entry)
    ask.return_value = (FAILED, {})
    await _checks(hass, coordinator, 2)  # one failure short of an alert
    gate = asyncio.Event()

    async def slow_failed(*args, **kwargs):
        await gate.wait()
        return FAILED, {}

    ask.side_effect = slow_failed
    first = hass.async_create_task(coordinator.async_check_now())  # the second failure, crosses the threshold
    await asyncio.sleep(0)
    ask.side_effect = None
    ask.return_value = (CLEAN, {})
    second = hass.async_create_task(coordinator.async_refresh())  # waits for the first
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)
    await hass.async_block_till_done()
    assert "spaghetti 92%" in notifications[0].data["message"]


# --- setup ----------------------------------------------------------------------------------------


async def test_setup_does_not_wait_for_the_first_check(hass, make_entry):
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()
        return CLEAN, {}

    hass.states.async_set(STATUS, "running")
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch(f"{COORDINATOR}.ask", side_effect=slow):
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get("sensor.p1s_last_check").state == "idle"
        gate.set()
        await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("sensor.p1s_last_check").state == "clean"


@pytest.mark.parametrize("method", ["_load", "_cleanup"])
async def test_disk_errors_do_not_stop_monitoring(hass, make_entry, ask, method):
    with patch.object(FrameHistory, method, side_effect=OSError(28, "No space left on device")):
        entry, _ = await _setup(hass, make_entry)
    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.p1s_last_check").state == "clean"


async def test_text_cost_is_parsed(hass, make_entry, ask, hass_ws_client):
    ask.return_value = (CLEAN, {"cost": "0.0002"})
    entry, coordinator = await _setup(hass, make_entry)
    assert coordinator.history.records[-1]["cost"] == 0.0002
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "print_failure_monitor/summary", "entry_id": entry.entry_id, "hours": 24})
    msg = await client.receive_json()
    assert msg["success"] and msg["result"]["stats"]["cost"] == 0.0002


async def test_check_now_result_stays_until_the_next_print(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry, status="finish")
    ask.return_value = (FAILED, {})
    await coordinator.async_check_now()
    await _checks(hass, coordinator, 2)  # scheduled ticks between prints
    assert hass.states.get("sensor.p1s_last_check").state == "failed"
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"  # shown, not counted

    ask.return_value = (CLEAN, {})
    hass.states.async_set(STATUS, "running")
    await _checks(hass, coordinator, 1)
    assert hass.states.get("sensor.p1s_last_check").state == "clean"
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert hass.states.get("sensor.p1s_last_check").state == "idle"


# --- monitoring down ------------------------------------------------------------------------------


def _titled(notifications, title):
    return [n.data["message"] for n in notifications if n.data.get("title") == title]


async def test_monitoring_down_alert_and_recovery(hass, make_entry, ask, notifications):
    entry, coordinator = await _setup(hass, make_entry)
    down_id = f"print_failure_monitor_{entry.entry_id}_down"
    ask.side_effect = RuntimeError("Cannot connect to host")
    await _checks(hass, coordinator, 4)
    assert _titled(notifications, "Print monitoring isn't working") == []
    await _checks(hass, coordinator, 3)  # the 5th failure alerts, later ones don't repeat it

    down = _titled(notifications, "Print monitoring isn't working")
    assert len(down) == 1 and "last 5 checks of P1S failed" in down[0] and "Cannot connect to host" in down[0]
    assert down_id in _async_get_or_create_notifications(hass)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"  # errors never count as failures

    issue = ir.async_get(hass).async_get_issue("print_failure_monitor", f"checks_failing_{entry.entry_id}")
    assert issue is not None and issue.translation_placeholders["error"] == "RuntimeError: Cannot connect to host"

    ask.side_effect = None
    ask.return_value = (CLEAN, {})
    await _checks(hass, coordinator, 2)
    assert _titled(notifications, "Print monitoring") == ["Checks of P1S are working again."]
    assert down_id not in _async_get_or_create_notifications(hass)
    assert ir.async_get(hass).async_get_issue("print_failure_monitor", f"checks_failing_{entry.entry_id}") is None


async def test_monitoring_down_needs_failures_in_a_row(hass, make_entry, ask, notifications):
    _, coordinator = await _setup(hass, make_entry)
    for _ in range(2):
        ask.side_effect = RuntimeError("HTTP 503")
        await _checks(hass, coordinator, 4)
        ask.side_effect = None
        await _checks(hass, coordinator, 1)
    assert _titled(notifications, "Print monitoring isn't working") == []


async def test_monitoring_down_closed_when_the_print_ends(hass, make_entry, ask, notifications):
    entry, coordinator = await _setup(hass, make_entry)
    ask.side_effect = RuntimeError("HTTP 503")
    await _checks(hass, coordinator, 5)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert _titled(notifications, "Print monitoring") == ["The print on P1S ended while checks were failing."]
    assert f"print_failure_monitor_{entry.entry_id}_down" not in _async_get_or_create_notifications(hass)


# --- rejected key, stage sensor, restarts ----------------------------------------------------------


async def test_rejected_key_starts_reauth(hass, make_entry, ask):
    _entry, coordinator = await _setup(hass, make_entry)
    ask.side_effect = AuthError("HTTP 401: invalid key")
    await _checks(hass, coordinator, 2)
    flows = hass.config_entries.flow.async_progress_by_handler("print_failure_monitor")
    assert [f["context"]["source"] for f in flows] == ["reauth"]  # one prompt, not one per check
    assert hass.states.get("sensor.p1s_last_check").state == "error"


async def test_stage_sensor_skips_the_start_routine(hass, make_entry, ask):
    hass.states.async_set("sensor.p1s_current_stage", "auto_bed_leveling")
    _, coordinator = await _setup(hass, make_entry, status="finish")
    hass.config_entries.async_update_entry(
        coordinator.config_entry, data={**coordinator.config_entry.data, "stage": "sensor.p1s_current_stage"}
    )
    hass.states.async_set(STATUS, "running")
    calls = ask.call_count
    await _checks(hass, coordinator, 2)
    assert ask.call_count == calls
    assert hass.states.get("sensor.p1s_last_check").state == "preparing"

    hass.states.async_set("sensor.p1s_current_stage", "printing")
    await _checks(hass, coordinator, 1)
    assert ask.call_count == calls + 1

    hass.states.async_set("sensor.p1s_current_stage", "unavailable")  # never blocks checks
    await _checks(hass, coordinator, 1)
    assert ask.call_count == calls + 2


async def test_alert_survives_a_restart(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry, auto_pause=True)
    _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    resume = _action(notifications[0], "Resume")
    hass.states.async_set(STATUS, "pause")

    assert await hass.config_entries.async_reload(entry.entry_id)  # as on a restart
    await hass.async_block_till_done(wait_background_tasks=True)
    presses = _mock_presses(hass)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    assert hass.states.get("sensor.p1s_last_check").state == "paused"

    await _tap(hass, resume)  # the old notification's button still works
    assert [p.data["entity_id"] for p in presses] == ["button.p1s_resume"]


async def test_old_saved_state_is_ignored(hass, make_entry, ask, notifications, freezer):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.tick(timedelta(hours=2))  # a long outage: probably not the same print any more
    ask.return_value = (CLEAN, {})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"


# --- pacing, plate area, frame comparison, daily limit, print report ------------------------------


async def test_checks_slow_down_after_the_early_phase(hass, make_entry, ask, freezer):
    _, coordinator = await _setup(hass, make_entry, interval=30, early_minutes=15, later_interval=90)
    assert coordinator.update_interval == timedelta(seconds=30)
    freezer.tick(timedelta(minutes=16))
    await _checks(hass, coordinator, 1)
    assert coordinator.update_interval == timedelta(seconds=90)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert coordinator.update_interval == timedelta(seconds=30)  # the next print starts fast again


async def test_early_phase_starts_after_the_start_routine(hass, make_entry, ask, freezer):
    hass.states.async_set("sensor.p1s_current_stage", "heatbed_preheating")
    _, coordinator = await _setup(
        hass, make_entry, data={"stage": "sensor.p1s_current_stage"}, early_minutes=15, later_interval=90
    )
    freezer.tick(timedelta(minutes=20))  # a long start routine doesn't use up the early phase
    hass.states.async_set("sensor.p1s_current_stage", "printing")
    await _checks(hass, coordinator, 1)
    assert coordinator.update_interval == timedelta(seconds=30)


async def test_plate_area_is_cropped(hass, make_entry, ask):
    await _setup(hass, make_entry, crop=[0, 0, 0.5, 0.5])
    image = ask.call_args.kwargs["image"]
    assert PILImage.open(io.BytesIO(image)).size == (455, 256)  # 320x180 of the 640x360 frame, short side to 256


async def test_frames_compared_layers_apart(hass, make_entry, ask, notifications):
    layer = "sensor.p1s_current_layer"
    hass.states.async_set(layer, "5")
    ask.return_value = (CLEAN, {})
    _, coordinator = await _setup(hass, make_entry, data={"layer": layer}, compare_layers=20)
    assert ask.call_args.kwargs["earlier"] is None
    assert "not_growing" not in ask.call_args.kwargs["questions"]
    first_frame = ask.call_args.kwargs["image"]

    hass.states.async_set(layer, "24")
    await _checks(hass, coordinator, 1)
    assert ask.call_args.kwargs["earlier"] is None  # only 19 layers apart

    stalled = {**CLEAN, "not_growing": 0.9}
    ask.return_value = (stalled, {})
    hass.states.async_set(layer, "26")
    await _checks(hass, coordinator, 3)  # 3 failed frames of 4 alert
    assert ask.call_args.kwargs["earlier"] == (first_frame, "image/jpeg", 21)
    assert "not_growing" in ask.call_args.kwargs["questions"]
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "on"
    assert "not growing 90%" in notifications[0].data["message"]


async def test_frames_not_compared_without_a_layer_sensor(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry, compare_layers=20)
    await _checks(hass, coordinator, 3)
    assert all(call.kwargs["earlier"] is None for call in ask.call_args_list)


async def test_daily_limit(hass, make_entry, ask, notifications):
    _, coordinator = await _setup(hass, make_entry, daily_limit=2)  # setup's check is the first
    await _checks(hass, coordinator, 3)
    assert ask.call_count == 2
    assert hass.states.get("sensor.p1s_last_check").state == "limit_reached"
    assert len(_titled(notifications, "Print monitoring paused")) == 1  # once a day, not once per check
    await coordinator.async_check_now()  # a manual check still works
    assert ask.call_count == 3


async def test_print_report(hass, make_entry, ask, notifications):
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 2)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    await hass.async_block_till_done()

    [report] = [n.data for n in notifications if n.data.get("title") == "Print watched"]
    assert "ended (finish)" in report["message"]
    assert "checked 3 times: 3 looked fine, 0 skipped and 0 showed a failure. No failure alerts." in report["message"]
    record = coordinator.history.records[-1]
    assert record["kind"] == "report" and report["data"]["image"].endswith(f"/{entry.entry_id}/{record['id']}")
    strip = PILImage.open(coordinator.history.frame_path(record["id"]))
    assert strip.height == 240 and strip.width > 3 * 400  # three frames side by side


async def test_print_report_can_be_turned_off(hass, make_entry, ask, notifications):
    _, coordinator = await _setup(hass, make_entry, print_report=False)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert _titled(notifications, "Print watched") == []


async def test_diagnostics_hide_the_key(hass, make_entry, ask, hass_client):
    entry, _ = await _setup(hass, make_entry)
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diagnostics["data"]["api_key"] == "**REDACTED**"
    assert diagnostics["snapshot"]["result"] == "clean" and "frame" not in diagnostics["snapshot"]
    assert diagnostics["history"]["records"] == 1


# --- found in the pre-release review ---------------------------------------------------------------


async def test_resuming_at_the_printer_counts_as_continue(hass, make_entry, ask, notifications):
    """Paused after an alert, then resumed on the printer: a later failure in the same print still alerts."""
    ask.return_value = (FAILED, {})
    _, coordinator = await _setup(hass, make_entry, auto_pause=True, snooze_minutes=1)
    presses = _mock_presses(hass)
    await _checks(hass, coordinator, 2)
    assert len(presses) == 1  # auto-paused
    hass.states.async_set(STATUS, "pause")
    await _checks(hass, coordinator, 1)
    hass.states.async_set(STATUS, "running")  # resumed on the printer's screen
    await _checks(hass, coordinator, 1)
    assert hass.states.get("binary_sensor.p1s_print_failure").state == "off"
    assert hass.states.get("sensor.p1s_last_check").state == "snoozed"
    assert coordinator.history.records[-1]["action"] == "RESUMED_AT_PRINTER"

    coordinator._snoozed_until = None  # the snooze ran out; the print fails again later
    await _checks(hass, coordinator, 3)
    assert len(presses) == 2 and len(_titled(notifications, "Print failure detected")) == 2


async def test_pause_and_resume_without_an_alert_changes_nothing(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry)
    hass.states.async_set(STATUS, "pause")
    await _checks(hass, coordinator, 1)
    hass.states.async_set(STATUS, "running")
    await _checks(hass, coordinator, 1)
    assert hass.states.get("sensor.p1s_last_check").state == "clean"  # not snoozed


async def test_daily_count_survives_a_long_restart(hass, make_entry, ask, notifications, freezer):
    freezer.move_to("2026-11-27 09:00:00+00:00")
    entry, coordinator = await _setup(hass, make_entry, daily_limit=2)
    await _checks(hass, coordinator, 2)
    assert await hass.config_entries.async_unload(entry.entry_id)
    freezer.tick(timedelta(hours=2))  # longer than the alert state is kept, same day
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await _checks(hass, entry.runtime_data, 2)
    assert ask.call_count == 2
    assert len(_titled(notifications, "Print monitoring paused")) == 1


async def test_daily_limit_starts_over_the_next_day(hass, make_entry, ask, notifications, freezer):
    freezer.move_to("2026-11-27 12:00:00+00:00")
    _, coordinator = await _setup(hass, make_entry, daily_limit=1)
    await _checks(hass, coordinator, 1)
    assert ask.call_count == 1
    freezer.tick(timedelta(days=1))
    await _checks(hass, coordinator, 1)
    assert ask.call_count == 2


async def test_monitoring_down_shown_again_after_a_restart(hass, make_entry, ask, notifications):
    ask.side_effect = RuntimeError("HTTP 500: down")
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 5)
    issue_id = f"checks_failing_{entry.entry_id}"
    assert await hass.config_entries.async_unload(entry.entry_id)
    ir.async_get(hass).async_delete("print_failure_monitor", issue_id)  # neither survives a restart
    _async_get_or_create_notifications(hass).clear()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    issue = ir.async_get(hass).async_get_issue("print_failure_monitor", issue_id)
    assert issue is not None and "HTTP 500: down" in issue.translation_placeholders["error"]
    assert f"print_failure_monitor_{entry.entry_id}_down" in _async_get_or_create_notifications(hass)
    assert len(_titled(notifications, "Print monitoring isn't working")) == 1  # the phone isn't told twice


async def test_removing_the_printer_clears_its_alerts(hass, make_entry, ask, notifications):
    ask.side_effect = RuntimeError("HTTP 500: down")
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 5)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue("print_failure_monitor", f"checks_failing_{entry.entry_id}") is None
    assert not [n for n in _async_get_or_create_notifications(hass) if entry.entry_id in n]


async def test_no_alert_on_a_clean_frame_after_lowering_the_requirement(hass, make_entry, ask, notifications):
    ask.return_value = (FAILED, {})
    entry, coordinator = await _setup(hass, make_entry)
    await _checks(hass, coordinator, 1)  # 2 failed frames, 3 needed
    ask.return_value = (CLEAN, {})
    hass.config_entries.async_update_entry(entry, options={**entry.options, "required": 2})
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert notifications == []  # the window already meets 2, but this frame looks fine
    ask.return_value = (FAILED, {})
    await _checks(hass, entry.runtime_data, 1)
    assert len(_titled(notifications, "Print failure detected")) == 1


async def test_check_now_waiting_behind_a_check_doesnt_count_after_a_snooze(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry)
    gate = asyncio.Event()

    async def slow(*_args, **_kwargs):
        await gate.wait()
        return FAILED, {}

    ask.side_effect = slow
    first = hass.async_create_task(coordinator.async_refresh())
    await asyncio.sleep(0)
    second = hass.async_create_task(coordinator.async_check_now())  # waits for the first check
    await asyncio.sleep(0)
    await coordinator.async_snooze(15)
    gate.set()
    await first
    await second
    assert coordinator.data.recent == []


async def test_idle_polls_dont_write_the_store(hass, make_entry, ask):
    _, coordinator = await _setup(hass, make_entry, status="idle")
    with patch.object(coordinator._store, "async_delay_save") as save:
        await _checks(hass, coordinator, 5)
    assert save.call_count == 0


async def test_state_is_saved_on_changes_and_during_a_print(hass, make_entry, ask, freezer):
    _, coordinator = await _setup(hass, make_entry)
    with patch.object(coordinator._store, "async_delay_save") as save:
        await _checks(hass, coordinator, 1)  # a check changes the call count
        assert save.call_count == 1
        coordinator.async_update_listeners()  # nothing changed
        assert save.call_count == 1
        freezer.tick(timedelta(minutes=11))
        coordinator.async_update_listeners()  # the heartbeat keeps saved_at recent during a print
        assert save.call_count == 2


async def test_a_bad_stored_questions_value_still_loads(hass, make_entry, ask):
    entry, coordinator = await _setup(hass, make_entry, questions=None)
    assert entry.state is ConfigEntryState.LOADED
    assert "spaghetti" in coordinator.questions


async def test_comparison_frames_reset_at_print_end_and_on_crop_change(hass, make_entry, ask):
    layer = "sensor.p1s_current_layer"
    hass.states.async_set(layer, "5")
    _, coordinator = await _setup(hass, make_entry, data={"layer": layer}, compare_layers=20)
    assert coordinator._earlier
    await coordinator.async_set_option("crop", [0, 0.3, 1, 1])
    assert coordinator._earlier == []
    await _checks(hass, coordinator, 1)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    assert coordinator._earlier == []
    hass.states.async_set(layer, "unavailable")
    hass.states.async_set(STATUS, "running")
    await _checks(hass, coordinator, 2)
    assert coordinator._earlier == [] and ask.call_args.kwargs["earlier"] is None


async def test_print_report_without_saved_frames(hass, make_entry, ask, notifications):
    _, coordinator = await _setup(hass, make_entry, save_frames="off")
    ask.return_value = ({**CLEAN, "spaghetti": 0.5}, {})  # unsure
    await _checks(hass, coordinator, 1)
    hass.states.async_set(STATUS, "finish")
    await _checks(hass, coordinator, 1)
    await hass.async_block_till_done()
    [report] = [n.data for n in notifications if n.data.get("title") == "Print watched"]
    assert "checked 2 times: 1 looked fine, 1 skipped and 0 showed a failure" in report["message"]
    assert "image" not in report["data"]
