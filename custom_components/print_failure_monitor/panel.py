"""Sidebar panel: static JS, a websocket API for history and stats, and a signed-URL frame endpoint."""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import probatio as vol
import yaml
from aiohttp import web
from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.components.http.auth import async_sign_path
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.http import KEY_HASS
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import dump

from .config_flow import validate_questions
from .const import CONF_CAMERA, CONF_CROP, CONF_NO, CONF_QUESTIONS, CONF_RETENTION_DAYS, CONF_YES, DOMAIN
from .history import LABEL_VALUES
from .questions import NOT_GROWING, VISIBLE, build_questions, judge

PANEL_URL = "print-failure-monitor"
STATIC_URL = f"/{DOMAIN}_static"
FRAME_URL = f"/api/{DOMAIN}/frame"
MAX_SERIES_POINTS = 600
RESULT_ORDER = ["failed", "unsure", "not_visible", "error", "clean"]
# Testing new questions asks the model again for each labeled frame; this bounds the cost of one run.
MAX_RETEST_FRAMES = 100


async def async_setup_panel(hass: HomeAssistant, version: str) -> None:
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), True)]
    )
    hass.http.register_view(FrameView())
    websocket_api.async_register_command(hass, ws_entries)
    websocket_api.async_register_command(hass, ws_summary)
    websocket_api.async_register_command(hass, ws_frames)
    websocket_api.async_register_command(hass, ws_frame)
    websocket_api.async_register_command(hass, ws_label)
    websocket_api.async_register_command(hass, ws_set_crop)
    websocket_api.async_register_command(hass, ws_settings)
    websocket_api.async_register_command(hass, ws_try_settings)
    websocket_api.async_register_command(hass, ws_save_settings)
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name="print-failure-monitor-panel",
        sidebar_title="Print failures",
        sidebar_icon="mdi:printer-3d-nozzle-alert",
        module_url=f"{STATIC_URL}/panel.js?v={version}",
        require_admin=False,
    )


def _coordinator(hass: HomeAssistant, entry_id: str):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.domain != DOMAIN or entry.state is not ConfigEntryState.LOADED:
        return None
    return entry.runtime_data


def _top(record: dict) -> float | None:
    failures = [p for q, p in record.get("answers", {}).items() if q != VISIBLE]
    return max(failures) if failures else None


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def label_stats(records: list[dict], labels: dict[str, str]) -> dict:
    """How the checks compare with your labels, over every labeled check (they're never cleaned up)."""
    labeled = [(r, labels[r["id"]]) for r in records if r["kind"] == "check" and r["id"] in labels]
    return {
        "total": len(labeled),
        "false_alarms": sum(1 for r, label in labeled if label == "fine" and r["result"] == "failed"),
        "missed": sum(1 for r, label in labeled if label == "failure" and r["result"] != "failed"),
    }


def summarize(records: list[dict], since: datetime) -> dict:
    """Stats, results per local day, and a failure-probability series downsampled for charting."""
    start = since.isoformat()
    checks = [r for r in records if r["kind"] == "check" and r["at"] >= start]
    events = [r for r in records if r["kind"] != "check" and r["at"] >= start]

    by_result = Counter(r["result"] for r in checks)
    daily: dict[str, Counter] = {}
    for r in checks:
        day = dt_util.as_local(datetime.fromisoformat(r["at"])).date().isoformat()
        daily.setdefault(day, Counter())[r["result"]] += 1

    points = [{"at": r["at"], "p": round(_top(r), 4), "result": r["result"]} for r in checks if _top(r) is not None]
    if len(points) > MAX_SERIES_POINTS:  # keep each bucket's highest point so spikes survive
        size = -(-len(points) // MAX_SERIES_POINTS)
        points = [max(points[i : i + size], key=lambda p: p["p"]) for i in range(0, len(points), size)]

    return {
        "stats": {
            "checks": len(checks),
            "by_result": {k: by_result.get(k, 0) for k in RESULT_ORDER},
            "alerts": sum(1 for e in events if e["kind"] == "alert"),
            "dismissed": sum(1 for e in events if e["kind"] == "action" and e.get("action") in ("CONTINUE", "RESUME")),
            "prints": len({r["print_id"] for r in checks if r.get("print_id")}),
            # None when the provider reports no cost (Google, OpenAI), so the panel can say so instead of $0.
            "cost": round(sum(costs), 6)
            if (costs := [r["cost"] for r in checks if _is_number(r.get("cost"))])
            else None,
        },
        "daily": [{"day": day, **{k: c.get(k, 0) for k in RESULT_ORDER}} for day, c in sorted(daily.items())],
        "series": points,
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/entries"})
@callback
def ws_entries(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    entries = [
        {
            "entry_id": e.entry_id,
            "title": e.title,
            "retention_days": e.runtime_data.option(CONF_RETENTION_DAYS),
            "camera": e.data[CONF_CAMERA],
            "crop": e.runtime_data.option(CONF_CROP),
        }
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.state is ConfigEntryState.LOADED
    ]
    connection.send_result(msg["id"], entries)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/summary",
        vol.Required("entry_id"): str,
        vol.Required("hours"): vol.All(int, vol.Range(min=1, max=24 * 366)),
    }
)
@websocket_api.async_response
async def ws_summary(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    result = summarize(coordinator.history.records, dt_util.utcnow() - timedelta(hours=msg["hours"]))
    result["stats"]["storage_bytes"] = await coordinator.history.async_storage_bytes()
    result["stats"]["labels"] = label_stats(coordinator.history.records, coordinator.history.labels)
    result["yes_threshold"] = coordinator.option(CONF_YES)
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/frames",
        vol.Required("entry_id"): str,
        vol.Required("hours"): vol.All(int, vol.Range(min=1, max=24 * 366)),
        vol.Optional("results"): [str],
        vol.Optional("labeled", default=False): bool,  # only labeled checks, from any time
        vol.Optional("before"): vol.All(str, vol.Match(r"^\d+$")),
        vol.Optional("limit", default=48): vol.All(int, vol.Range(min=1, max=200)),
    }
)
@callback
def ws_frames(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Checks newest first, with a signed image URL where the frame was kept."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    start = (dt_util.utcnow() - timedelta(hours=msg["hours"])).isoformat()
    results = set(msg.get("results") or [])
    before = msg.get("before")
    labels = coordinator.history.labels
    out = []
    for r in reversed(coordinator.history.records):
        if r["at"] < start and not msg["labeled"]:
            break
        if r["kind"] != "check" or (results and r["result"] not in results):
            continue
        if msg["labeled"] and r["id"] not in labels:
            continue
        if before and int(r["id"]) >= int(before):
            continue
        out.append(_with_url(hass, msg["entry_id"], r, labels))
        if len(out) >= msg["limit"]:
            break
    connection.send_result(msg["id"], {"frames": out, "more": len(out) >= msg["limit"]})


def _with_url(hass: HomeAssistant, entry_id: str, record: dict, labels: dict[str, str]) -> dict:
    url = None
    if record.get("frame"):
        url = async_sign_path(hass, f"{FRAME_URL}/{entry_id}/{record['id']}", timedelta(hours=1))
    return {**record, "top": _top(record), "url": url, "label": labels.get(record["id"])}


def _find_check(coordinator, frame_id: str) -> dict | None:
    return next(
        (r for r in reversed(coordinator.history.records) if r["kind"] == "check" and r["id"] == frame_id), None
    )


@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/frame", vol.Required("entry_id"): str, vol.Required("frame_id"): str}
)
@callback
def ws_frame(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """One check by id, for opening the panel from a notification."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    if (record := _find_check(coordinator, msg["frame_id"])) is None:
        connection.send_error(msg["id"], "not_found", "That check is no longer in the history")
        return
    connection.send_result(msg["id"], _with_url(hass, msg["entry_id"], record, coordinator.history.labels))


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/label",
        vol.Required("entry_id"): str,
        vol.Required("frame_id"): vol.All(str, vol.Match(r"^\d+$")),
        vol.Required("label"): vol.Any(None, *LABEL_VALUES),
    }
)
@websocket_api.require_admin  # labeled frames are exempt from cleanup and change the settings tests
@websocket_api.async_response
async def ws_label(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Set what a checked frame really showed (failure or fine), or clear it."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    if _find_check(coordinator, msg["frame_id"]) is None:
        connection.send_error(msg["id"], "not_found", "That check is no longer in the history")
        return
    await coordinator.history.async_set_label(msg["frame_id"], msg["label"])
    connection.send_result(msg["id"], {"label": msg["label"]})


def _crop(value):
    """[left, top, right, bottom] as fractions of the frame, at least a tenth of it each way."""
    if value is None:
        return None
    left, top, right, bottom = (float(v) for v in value)
    if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1) or right - left < 0.1 or bottom - top < 0.1:
        raise vol.Invalid("crop must be inside the frame and at least a tenth of it each way")
    # A thin strip would be enlarged far past the size limit to keep its short side readable.
    if not 0.25 <= (right - left) / (bottom - top) <= 4:
        raise vol.Invalid("crop can't be more than 4 times wider than tall, or the other way round")
    return [round(v, 4) for v in (left, top, right, bottom)]


@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_crop",
        vol.Required("entry_id"): str,
        vol.Required("crop"): vol.All(vol.Any(None, vol.All([vol.Coerce(float)], vol.Length(min=4, max=4))), _crop),
    }
)
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_crop(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Set the plate area that checks look at, or None for the whole frame. Applies from the next check."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    await coordinator.async_set_option(CONF_CROP, msg["crop"])
    connection.send_result(msg["id"], {"crop": msg["crop"]})


def _read_frame(history, frame_id: str) -> bytes:
    if (path := history.frame_path(frame_id)) is None:
        raise FileNotFoundError("the frame is no longer saved")
    return path.read_bytes()


def _questions_yaml(questions: dict) -> str:
    return dump(questions) if questions else ""


def _parse_settings(msg: dict) -> tuple[float, float, dict]:
    """Validate thresholds and questions YAML from the panel the same way the options form does."""
    if msg["no"] >= msg["yes"]:
        raise ValueError("The no threshold must be below the yes threshold.")
    try:
        # Plain YAML: Home Assistant's loader would also resolve tags such as !env_var.
        parsed = yaml.safe_load(msg["questions"])
    except yaml.YAMLError as err:
        raise ValueError(f"Questions aren't valid YAML: {err}") from err
    # Use what validation returns: text that's only a comment parses to None, which means no overrides.
    return msg["yes"], msg["no"], validate_questions(parsed)


SETTINGS_SCHEMA = {
    vol.Required("entry_id"): str,
    vol.Required("yes"): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=0.98)),
    vol.Required("no"): vol.All(vol.Coerce(float), vol.Range(min=0.02, max=0.95)),
    vol.Required("questions"): str,
}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/settings", vol.Required("entry_id"): str})
@callback
def ws_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """The settings the test dialog starts from, with how many labeled frames it would use."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    labels = coordinator.history.labels
    labeled = [r for r in coordinator.history.records if r["kind"] == "check" and r["id"] in labels]
    connection.send_result(
        msg["id"],
        {
            "yes": coordinator.option(CONF_YES),
            "no": coordinator.option(CONF_NO),
            "questions": _questions_yaml(coordinator.option(CONF_QUESTIONS)),
            "labeled": len(labeled),
            "with_frames": min(sum(1 for r in labeled if r.get("frame")), MAX_RETEST_FRAMES),
        },
    )


def _errors(rows: list[dict], key: str) -> dict:
    return {
        "false_alarms": sum(1 for r in rows if r["label"] == "fine" and r[key] == "failed"),
        "missed": sum(1 for r in rows if r["label"] == "failure" and r[key] != "failed"),
    }


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/try_settings", **SETTINGS_SCHEMA})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_try_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Judge every labeled frame with the current and the proposed settings, without saving anything.

    Thresholds alone are applied to the stored answers. Changed questions mean asking the model again for each
    labeled frame that was saved, which costs one call each.
    """
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    try:
        yes, no, overrides = _parse_settings(msg)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    questions, _ = build_questions(overrides)
    reask = questions != coordinator.questions
    history = coordinator.history
    labeled = [r for r in history.records if r["kind"] == "check" and r["id"] in history.labels and r["answers"]]
    if reask:
        labeled = [r for r in labeled if r.get("frame")][-MAX_RETEST_FRAMES:]
    current_yes, current_no = coordinator.option(CONF_YES), coordinator.option(CONF_NO)
    limit = asyncio.Semaphore(2)  # free tiers rate-limit bursts

    async def row(record: dict) -> dict:
        answers, error = record["answers"], None
        if reask:
            try:
                async with limit:
                    image = await hass.async_add_executor_job(_read_frame, history, record["id"])
                    answers, _ = await coordinator.async_ask_image(image, "image/jpeg", questions)
                # The comparison needs the frame from layers earlier, which isn't kept; reuse its stored answer
                # so a frame judged on it compares like for like.
                if NOT_GROWING in record["answers"]:
                    answers = {**answers, NOT_GROWING: record["answers"][NOT_GROWING]}
            except Exception as err:  # noqa: BLE001  shown on that row
                error = f"{type(err).__name__}: {err}"
        return {
            **_with_url(hass, msg["entry_id"], record, history.labels),
            "before": judge(record["answers"], current_yes, current_no)[0],
            "after": "error" if error else judge(answers, yes, no)[0],
            "new_answers": None if error else answers,
            "error": error,
        }

    rows = await asyncio.gather(*(row(r) for r in labeled))
    connection.send_result(
        msg["id"],
        {
            "rows": rows,
            "asked": len(rows) if reask else 0,
            "current": _errors(rows, "before"),
            "proposed": _errors(rows, "after"),
        },
    )


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/save_settings", **SETTINGS_SCHEMA})
@websocket_api.require_admin
@callback
def ws_save_settings(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    """Save thresholds and questions tried in the panel. The printer reloads to use them."""
    if (coordinator := _coordinator(hass, msg["entry_id"])) is None:
        connection.send_error(msg["id"], "not_found", "Printer not found")
        return
    try:
        yes, no, questions = _parse_settings(msg)
    except ValueError as err:
        connection.send_error(msg["id"], "invalid", str(err))
        return
    entry = coordinator.config_entry
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_YES: yes, CONF_NO: no, CONF_QUESTIONS: questions}
    )
    hass.config_entries.async_schedule_reload(entry.entry_id)
    connection.send_result(msg["id"], {})


class FrameView(HomeAssistantView):
    """Serves saved frames. Requires auth; the panel uses signed URLs so <img> tags work."""

    url = FRAME_URL + "/{entry_id}/{frame_id}"
    name = f"api:{DOMAIN}:frame"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, frame_id: str) -> web.StreamResponse:
        hass = request.app[KEY_HASS]
        if (coordinator := _coordinator(hass, entry_id)) is None:
            return web.Response(status=404)
        if (path := await hass.async_add_executor_job(coordinator.history.frame_path, frame_id)) is None:
            return web.Response(status=404)
        return web.FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
