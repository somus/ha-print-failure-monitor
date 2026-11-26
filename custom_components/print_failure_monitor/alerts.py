"""Failure notifications: a persistent notification in Home Assistant, plus actionable phone notifications."""

from __future__ import annotations

import io
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components import persistent_notification
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from PIL import Image

from .const import (
    ACTION_CONTINUE,
    ACTION_PAUSE,
    ACTION_PREFIX,
    ACTION_RESUME,
    ACTION_STOP,
    CONF_CAMERA,
    CONF_NOTIFY,
    CONF_PAUSE_BUTTON,
    CONF_RESUME_BUTTON,
    CONF_STOP_BUTTON,
    CONF_YES,
    DOMAIN,
)
from .panel import FRAME_URL, PANEL_URL
from .questions import failure_ids

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from .coordinator import PrintFailureCoordinator

_LOGGER = logging.getLogger(__name__)

# Fired by the Home Assistant Companion app when a notification button is tapped.
MOBILE_ACTION_EVENT = "mobile_app_notification_action"
# How long the frame link in a notification works. Phones download the image on delivery.
FRAME_LINK_LIFETIME = timedelta(hours=24)


def _tag(entry: ConfigEntry) -> str:
    """Id of the persistent notification and tag of the phone notification, so either can be replaced or cleared."""
    return f"{DOMAIN}_{entry.entry_id}"


def _action_id(entry: ConfigEntry, action: str) -> str:
    return f"{ACTION_PREFIX}{entry.entry_id}_{action}"


def _actions(entry: ConfigEntry, paused: bool) -> list[dict[str, str]]:
    data = entry.data
    buttons = []
    if paused:
        if data.get(CONF_RESUME_BUTTON):
            buttons.append((ACTION_RESUME, "Resume"))
    elif data.get(CONF_PAUSE_BUTTON):
        buttons.append((ACTION_PAUSE, "Pause"))
    if data.get(CONF_STOP_BUTTON):
        buttons.append((ACTION_STOP, "Stop"))
    if not paused:
        buttons.append((ACTION_CONTINUE, "Continue"))
    return [{"action": _action_id(entry, action), "title": title} for action, title in buttons]


def failure_message(coordinator: PrintFailureCoordinator, answers: dict[str, float], paused: bool) -> str:
    seen = ", ".join(
        f"{qid.replace('_', ' ')} {answers[qid]:.0%}"
        for qid in sorted(failure_ids(answers), key=lambda q: -answers[q])
        if answers[qid] > coordinator.option(CONF_YES)
    )
    outcome = "The printer was paused." if paused else "The printer is still running."
    return f"Possible print failure on {coordinator.config_entry.title}: {seen}. {outcome}"


@dataclass(frozen=True)
class FrameLinks:
    """Where the frame that triggered an alert can be fetched, or the live camera if it wasn't saved."""

    path: str  # for the companion apps, which send their own login with any path starting with /
    signed: str | None  # works without a login, for browsers; None when the frame wasn't saved
    panel: str  # the Print failures panel opened on that frame


@callback
def frame_links(hass: HomeAssistant, entry: ConfigEntry, frame_id: str | None) -> FrameLinks:
    if frame_id:
        path = f"{FRAME_URL}/{entry.entry_id}/{frame_id}"
        return FrameLinks(
            path,
            async_sign_path(hass, path, FRAME_LINK_LIFETIME),
            f"/{PANEL_URL}?entry={entry.entry_id}&frame={frame_id}",
        )
    return FrameLinks(f"/api/camera_proxy/{entry.data[CONF_CAMERA]}", None, f"/{PANEL_URL}?entry={entry.entry_id}")


async def async_send_failure_notification(
    coordinator: PrintFailureCoordinator, answers: dict[str, float], paused: bool, links: FrameLinks
) -> None:
    """Notify with the frame that triggered the alert."""
    hass = coordinator.hass
    entry = coordinator.config_entry
    title = "Print failure detected"
    message = failure_message(coordinator, answers, paused)

    # A browser <img> sends no login, so the persistent notification needs the signed link; without a saved
    # frame there's none, and it shows no picture.
    picture = f"\n\n![Frame that triggered the alert]({links.signed})" if links.signed else ""
    persistent_notification.async_create(
        hass,
        f"{message}{picture}\n\n[Open in the Print failures panel]({links.panel})",
        title,
        notification_id=_tag(entry),
    )

    payload = {
        "title": title,
        "message": message,
        "data": {
            "actions": _actions(entry, paused),
            # The plain path, not the signed link: the iOS app escapes the "?" of a query string into the
            # path, which breaks the signature (and gives a 404).
            "image": links.path,
            "tag": _tag(entry),
            # Tapping the notification opens the panel on this frame: url on iOS, clickAction on Android.
            "url": links.panel,
            "clickAction": links.panel,
        },
    }
    await _notify(coordinator, payload)


async def _notify(coordinator: PrintFailureCoordinator, payload: dict, mobile_only: bool = False) -> None:
    """Send to every configured notify service; one bad target shouldn't stop the others."""
    for notify in coordinator.option(CONF_NOTIFY):
        service = notify.removeprefix("notify.")
        if mobile_only and not service.startswith("mobile_app_"):
            continue
        try:
            await coordinator.hass.services.async_call("notify", service, payload, blocking=True)
        except Exception as err:  # noqa: BLE001  report and carry on
            _LOGGER.error("Sending notification via notify.%s failed: %s", service, err)


async def async_clear_failure_notification(coordinator: PrintFailureCoordinator) -> None:
    """Remove the alert once it's over.

    That clears the persistent notification, and the phone notification where the app can clear it.
    """
    tag = _tag(coordinator.config_entry)
    persistent_notification.async_dismiss(coordinator.hass, tag)
    # Only the companion app understands clear_notification; other services would send the literal text.
    await _notify(coordinator, {"message": "clear_notification", "data": {"tag": tag}}, mobile_only=True)


def _down_tag(entry: ConfigEntry) -> str:
    return f"{_tag(entry)}_down"


def _issue_id(entry: ConfigEntry) -> str:
    return f"checks_failing_{entry.entry_id}"


DOWN_TITLE = "Print monitoring isn't working"


def _down_message(entry: ConfigEntry, checks: int, error: str) -> str:
    return (
        f"The last {checks} checks of {entry.title} failed, so this print isn't being watched for failures. "
        f"Last error: {error}"
    )


@callback
def show_monitoring_down(coordinator: PrintFailureCoordinator, checks: int, error: str) -> None:
    """The persistent notification and repair issue for failing checks. Neither survives a restart, so a
    restored monitoring-down state shows them again; the phone notification is still on the phone."""
    entry = coordinator.config_entry
    persistent_notification.async_create(
        coordinator.hass, _down_message(entry, checks, error), DOWN_TITLE, notification_id=_down_tag(entry)
    )
    # Also listed under Settings > Repairs, which stays visible after the notification is dismissed.
    ir.async_create_issue(
        coordinator.hass,
        DOMAIN,
        _issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="checks_failing",
        translation_placeholders={
            "printer": entry.title,
            "checks": str(checks),
            "camera": entry.data[CONF_CAMERA],
            "error": error[:300],
        },
    )


async def async_send_monitoring_down(coordinator: PrintFailureCoordinator, checks: int, error: str) -> None:
    """Checks keep failing during a print, so failures can't be detected. Says so instead of staying silent."""
    entry = coordinator.config_entry
    show_monitoring_down(coordinator, checks, error)
    await _notify(
        coordinator,
        {"title": DOWN_TITLE, "message": _down_message(entry, checks, error), "data": {"tag": _down_tag(entry)}},
    )


@callback
def async_remove_alerts(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove everything this printer may still show in Home Assistant, when it's removed."""
    persistent_notification.async_dismiss(hass, _tag(entry))
    persistent_notification.async_dismiss(hass, _down_tag(entry))
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry))


async def async_send_monitoring_restored(coordinator: PrintFailureCoordinator, print_ended: bool) -> None:
    """Close a monitoring-down alert; the phone notification is replaced through its tag."""
    entry = coordinator.config_entry
    persistent_notification.async_dismiss(coordinator.hass, _down_tag(entry))
    ir.async_delete_issue(coordinator.hass, DOMAIN, _issue_id(entry))
    message = (
        f"The print on {entry.title} ended while checks were failing."
        if print_ended
        else f"Checks of {entry.title} are working again."
    )
    await _notify(coordinator, {"title": "Print monitoring", "message": message, "data": {"tag": _down_tag(entry)}})


async def async_send_limit_reached(coordinator: PrintFailureCoordinator, limit: int) -> None:
    entry = coordinator.config_entry
    message = (
        f"{entry.title} used its {limit} checks for today, so the rest of today's printing isn't being watched. "
        "Checks start again tomorrow; the limit is under Configure."
    )
    await _notify(
        coordinator, {"title": "Print monitoring paused", "message": message, "data": {"tag": f"{_tag(entry)}_limit"}}
    )


# The print report shows up to this many saved frames side by side, each scaled to this height.
REPORT_FRAMES = 6
REPORT_HEIGHT = 240


def _strip(paths: list[Path]) -> bytes:
    """Saved frames side by side in one JPEG. Blocking; run in an executor."""
    images = []
    for path in paths:
        with Image.open(path) as img:
            images.append(img.convert("RGB").resize((round(img.width * REPORT_HEIGHT / img.height), REPORT_HEIGHT)))
    strip = Image.new("RGB", (sum(i.width for i in images) + 4 * (len(images) - 1), REPORT_HEIGHT), (0, 0, 0))
    x = 0
    for img in images:
        strip.paste(img, (x, 0))
        x += img.width + 4
    out = io.BytesIO()
    strip.save(out, "JPEG", quality=85)
    return out.getvalue()


def _duration(start: str) -> str:
    minutes = round((dt_util.utcnow() - dt_util.parse_datetime(start)).total_seconds() / 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


async def async_send_print_report(coordinator: PrintFailureCoordinator, print_id: str, status: str) -> None:
    """When a watched print ends, say how it went, with a strip of frames from across the print."""
    if not coordinator.option(CONF_NOTIFY):
        return
    history = coordinator.history
    records = [r for r in history.records if r.get("print_id") == print_id]
    checks = [r for r in records if r["kind"] == "check" and r.get("counted")]
    if not checks:
        return
    entry = coordinator.config_entry
    results = Counter(r["result"] for r in checks)
    skipped = len(checks) - results["clean"] - results["failed"]
    alerts = sum(1 for r in records if r["kind"] == "alert")
    message = (
        f"The print on {entry.title} ended ({status}) after {_duration(print_id)}. It was checked {len(checks)} "
        f"times: {results['clean']} looked fine, {skipped} skipped and {results['failed']} showed a failure. "
        + (f"{alerts} failure alert{'s were' if alerts > 1 else ' was'} sent." if alerts else "No failure alerts.")
    )
    data: dict = {"tag": f"{_tag(entry)}_report", "url": f"/{PANEL_URL}?entry={entry.entry_id}"}
    data["clickAction"] = data["url"]
    saved = [r["id"] for r in checks if r.get("frame")]
    if saved:
        picks = sorted({round(i * (len(saved) - 1) / (REPORT_FRAMES - 1)) for i in range(REPORT_FRAMES)})
        try:
            strip = await coordinator.hass.async_add_executor_job(
                _strip, [history.dir / f"{saved[i]}.jpg" for i in picks]
            )
            record = await history.async_add_report(dt_util.utcnow(), print_id, strip)
            data["image"] = f"{FRAME_URL}/{entry.entry_id}/{record['id']}"
        except OSError as err:
            _LOGGER.error("Saving the print report image failed: %s", err)
    await _notify(coordinator, {"title": "Print watched", "message": message, "data": data})


@callback
def async_listen_for_actions(hass: HomeAssistant, coordinator: PrintFailureCoordinator):
    """Route this entry's notification buttons to the coordinator. Returns the unsubscribe callback."""
    prefix = f"{ACTION_PREFIX}{coordinator.config_entry.entry_id}_"

    async def handle(event: Event) -> None:
        action = event.data.get("action", "")
        if action.startswith(prefix):
            persistent_notification.async_dismiss(hass, _tag(coordinator.config_entry))
            await coordinator.async_handle_action(action.removeprefix(prefix))

    return hass.bus.async_listen(MOBILE_ACTION_EVENT, handle)
