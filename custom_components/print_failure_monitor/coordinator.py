"""Checks camera frames on an interval and decides when a print has failed."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.components.camera import async_get_image
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .alerts import (
    async_clear_failure_notification,
    async_send_failure_notification,
    async_send_limit_reached,
    async_send_monitoring_down,
    async_send_monitoring_restored,
    async_send_print_report,
    frame_links,
    show_monitoring_down,
)
from .api import PROVIDERS, AuthError, ask, downscale
from .const import (
    ACTION_CONTINUE,
    ACTION_PAUSE,
    ACTION_RESUME,
    ACTION_STOP,
    CONF_ACTIVE_STATES,
    CONF_API_KEY,
    CONF_AUTO_PAUSE,
    CONF_BASE_URL,
    CONF_CAMERA,
    CONF_CAMERA_DESCRIPTION,
    CONF_COMPARE_LAYERS,
    CONF_CROP,
    CONF_DAILY_LIMIT,
    CONF_EARLY_MINUTES,
    CONF_ENABLED,
    CONF_INTERVAL,
    CONF_LATER_INTERVAL,
    CONF_LAYER,
    CONF_MAX_IMAGE_SIZE,
    CONF_MODEL,
    CONF_NO,
    CONF_PAUSE_BUTTON,
    CONF_PAUSED_STATES,
    CONF_PRINT_REPORT,
    CONF_PRINT_STATUS,
    CONF_PROVIDER,
    CONF_QUESTIONS,
    CONF_REQUIRED,
    CONF_RESUME_BUTTON,
    CONF_SAVE_FRAMES,
    CONF_SNOOZE,
    CONF_STAGE,
    CONF_STAGE_STATES,
    CONF_STOP_BUTTON,
    CONF_TIMEOUT,
    CONF_WINDOW,
    CONF_YES,
    DEFAULT_OPTIONS,
    DEFAULT_PAUSED_STATES,
    DEFAULT_STAGE_STATES,
    DOMAIN,
    EVENT_FAILURE,
)
from .history import FrameHistory
from .questions import COMPARE, VISIBLE, build_questions, failure_ids, judge

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

_LOGGER = logging.getLogger(__name__)

# Result of the latest check, shown by the "Last check" sensor.
RESULT_IDLE = "idle"  # not printing, or monitoring turned off
RESULT_SNOOZED = "snoozed"
RESULT_PAUSED = "paused"
RESULT_PREPARING = "preparing"  # printing, but the stage sensor says it's levelling, heating, changing filament...
RESULT_FAILED = "failed"
RESULT_CLEAN = "clean"
RESULT_UNSURE = "unsure"
RESULT_NOT_VISIBLE = "not_visible"
RESULT_ERROR = "error"
RESULT_LIMITED = "limit_reached"  # the daily limit on model calls is used up
RESULTS = [
    RESULT_IDLE,
    RESULT_SNOOZED,
    RESULT_PAUSED,
    RESULT_PREPARING,
    RESULT_FAILED,
    RESULT_CLEAN,
    RESULT_UNSURE,
    RESULT_NOT_VISIBLE,
    RESULT_ERROR,
    RESULT_LIMITED,
]


# What the print status entity says about the print.
PRINTING = "printing"
PAUSED = "paused"
UNKNOWN = "unknown"  # unavailable or missing, often a short connection drop
ENDED = "ended"
# How long an unavailable status may last before the print is treated as over.
UNKNOWN_GRACE = timedelta(minutes=10)
# Failed checks in a row during a print before saying monitoring isn't working (2.5 minutes at the default interval).
ERRORS_BEFORE_DOWN_ALERT = 5
# Alert state saved across restarts is used only if Home Assistant was down for less than this; after a longer
# gap the printer may well be on a different print.
RESTORE_WITHIN = timedelta(hours=1)
# During a print, the state is saved at least this often, so restore still works after a crash.
SAVE_HEARTBEAT = timedelta(minutes=10)
STATE_VERSION = 1


def _state_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STATE_VERSION, f"{DOMAIN}.{entry_id}")


async def async_remove_state(hass: HomeAssistant, entry_id: str) -> None:
    await _state_store(hass, entry_id).async_remove()


def _cost(usage: dict) -> float | None:
    try:
        return float(usage["cost"])
    except KeyError, TypeError, ValueError:
        return None


@dataclass
class Snapshot:
    """What the entities show."""

    result: str = RESULT_IDLE
    failing: bool = False
    answers: dict[str, float] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)
    checked_at: datetime | None = None
    error: str | None = None
    cost: float | None = None
    frame: bytes | None = None
    frame_type: str = "image/jpeg"
    frame_at: datetime | None = None


class PrintFailureCoordinator(DataUpdateCoordinator[Snapshot]):
    """Owns the check loop, the debounce window and the failure response."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.option_of(entry, CONF_INTERVAL)),
        )
        # Set before anything that can raise, since async_shutdown uses them.
        self._store = _state_store(hass, entry.entry_id)
        self._closed = False
        self.questions, _ = build_questions(self.option(CONF_QUESTIONS) or {})
        self._verdicts: deque[bool] = deque(maxlen=self.option(CONF_WINDOW))
        self._alerted = False
        self._paused_by_us = False
        self._snoozed_until: datetime | None = None
        self._print_id: str | None = None
        # When the current print got past its start routine; the check interval slows down after a while.
        self._printing_since: datetime | None = None
        # (layer, frame, content type) from this print, at most one per layer, for the not-growing comparison.
        self._earlier: list[tuple[int, bytes, str]] = []
        # Model calls today (local date), for the daily limit.
        self._calls = {"day": None, "count": 0}
        self._limit_noticed: str | None = None  # the day the limit notice was sent
        self._unknown_since: datetime | None = None
        # A Check now result from between prints, kept on show until the next print starts.
        self._showing_manual_check = False
        self._errors_in_a_row = 0
        self._down_alerted = False
        self._down_error: str | None = None  # the error the monitoring-down alert was raised for
        # A pause seen while a failure was alerted: resuming the print afterwards counts as Continue.
        self._paused_while_alerted = False
        self._saved: tuple[dict, datetime] | None = None  # last saved state (without its time) and when
        # Bumped whenever the alert state is reset, so a check started before that can't count afterwards.
        self._generation = 0
        self._check_lock = asyncio.Lock()
        self._last = Snapshot()
        self.history = FrameHistory(hass, entry.entry_id)

    @staticmethod
    def option_of(entry: ConfigEntry, key: str):
        return entry.options.get(key, DEFAULT_OPTIONS[key])

    def option(self, key: str):
        return self.option_of(self.config_entry, key)

    @property
    def model(self) -> str:
        return self.option(CONF_MODEL) or PROVIDERS[self.config_entry.data[CONF_PROVIDER]][1]

    def _print_state(self) -> str:
        data = self.config_entry.data
        state = self.hass.states.get(data[CONF_PRINT_STATUS])
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return UNKNOWN
        if state.state in data[CONF_ACTIVE_STATES]:
            return PRINTING
        if state.state in data.get(CONF_PAUSED_STATES, DEFAULT_PAUSED_STATES):
            return PAUSED
        return ENDED

    def _stage_ready(self) -> bool:
        """With a stage sensor set, check only in the stages that print. An unreadable stage doesn't block checks."""
        data = self.config_entry.data
        if not data.get(CONF_STAGE):
            return True
        state = self.hass.states.get(data[CONF_STAGE])
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return True
        return state.state in data.get(CONF_STAGE_STATES, DEFAULT_STAGE_STATES)

    # --- state kept across restarts -----------------------------------------------------------------

    async def async_restore(self) -> None:
        """Pick up an alert, snooze and failure window from before a restart, if it was a short one."""
        saved = await self._store.async_load()
        if not saved:
            return
        # Keyed by date, so valid after any gap; _calls_today() starts a new day itself.
        self._calls = saved.get("calls", self._calls)
        self._limit_noticed = saved.get("limit_noticed")
        if dt_util.utcnow() - dt_util.parse_datetime(saved["saved_at"]) > RESTORE_WITHIN:
            return
        self._print_id = saved["print_id"]
        self._alerted = saved["alerted"]
        self._paused_by_us = saved["paused_by_us"]
        self._snoozed_until = dt_util.parse_datetime(saved["snoozed_until"]) if saved["snoozed_until"] else None
        self._verdicts.extend(saved["verdicts"])
        self._down_alerted = saved["down_alerted"]
        self._down_error = saved.get("down_error")
        self._errors_in_a_row = saved["errors_in_a_row"]
        self._printing_since = dt_util.parse_datetime(saved["printing_since"]) if saved.get("printing_since") else None
        self._paused_while_alerted = saved.get("paused_while_alerted", False)
        if self._down_alerted:  # the repair issue and persistent notification don't survive a restart
            show_monitoring_down(self, self._errors_in_a_row, self._down_error or "unknown")
        self._last = Snapshot(failing=self._alerted, recent=["fail" if v else "ok" for v in self._verdicts])

    def saved_state(self) -> dict:
        return {
            "saved_at": dt_util.utcnow().isoformat(),
            "print_id": self._print_id,
            "alerted": self._alerted,
            "paused_by_us": self._paused_by_us,
            "snoozed_until": self._snoozed_until.isoformat() if self._snoozed_until else None,
            "verdicts": list(self._verdicts),
            "down_alerted": self._down_alerted,
            "down_error": self._down_error,
            "paused_while_alerted": self._paused_while_alerted,
            "errors_in_a_row": self._errors_in_a_row,
            "printing_since": self._printing_since.isoformat() if self._printing_since else None,
            "calls": self._calls,
            "limit_noticed": self._limit_noticed,
        }

    def async_update_listeners(self) -> None:
        """Every state change ends up here (refreshes and direct updates), so save alongside it.

        Only when the state changed, or every SAVE_HEARTBEAT during a print: saved_at is what restore checks.
        """
        super().async_update_listeners()
        now = dt_util.utcnow()
        state = {k: v for k, v in self.saved_state().items() if k != "saved_at"}
        if self._saved is None or state != self._saved[0] or (self._print_id and now - self._saved[1] > SAVE_HEARTBEAT):
            self._saved = (state, now)
            self._store.async_delay_save(self.saved_state, 1)

    def _snoozed(self) -> bool:
        return self._snoozed_until is not None and dt_util.utcnow() < self._snoozed_until

    def _snapshot(self, result: str, **changes) -> Snapshot:
        self._last = Snapshot(
            **{
                **self._last.__dict__,
                "result": result,
                "failing": self._alerted,
                "recent": ["fail" if v else "ok" for v in self._verdicts],
                **changes,
            }
        )
        return self._last

    async def _async_update_data(self) -> Snapshot:
        snapshot = await self._update()
        # Most failures start in the first layers, so check often early on and less often later.
        early = self._printing_since is None or dt_util.utcnow() - self._printing_since < timedelta(
            minutes=self.option(CONF_EARLY_MINUTES)
        )
        self.update_interval = timedelta(seconds=self.option(CONF_INTERVAL if early else CONF_LATER_INTERVAL))
        return snapshot

    async def _update(self) -> Snapshot:
        """Check during a print.

        A pause or a short status drop keeps the alert and snooze; only the print's end resets them.
        """
        state = self._print_state() if self.option(CONF_ENABLED) else ENDED
        if state == PAUSED:
            self._unknown_since = None
            self._paused_while_alerted = self._paused_while_alerted or self._alerted
            return self._snapshot(RESULT_PAUSED)
        if state == UNKNOWN and self._print_id is not None:
            now = dt_util.utcnow()
            self._unknown_since = self._unknown_since or now
            if now - self._unknown_since < UNKNOWN_GRACE:
                return self._snapshot(self._last.result)
        if state != PRINTING:
            self._end_print(report=self.option(CONF_ENABLED))
            return self._snapshot(self._last.result if self._showing_manual_check else RESULT_IDLE)
        self._unknown_since = None
        if self._print_id is None:
            self._print_id = dt_util.utcnow().isoformat()
            self._showing_manual_check = False
        if self._paused_while_alerted:
            # Paused after an alert and resumed at the printer, not from the notification: you've looked and
            # decided it's fine, so treat it as Continue. Otherwise the alert would stay latched and a later
            # failure in this print would go unreported.
            self._paused_while_alerted = False
            if self._alerted:
                await self._record_event("action", action="RESUMED_AT_PRINTER")
                self._snooze(self.option(CONF_SNOOZE))
        if self._snoozed():
            return self._snapshot(RESULT_SNOOZED)
        if not self._stage_ready():
            return self._snapshot(RESULT_PREPARING)
        self._printing_since = self._printing_since or dt_util.utcnow()
        if self._over_limit():
            return self._snapshot(RESULT_LIMITED)
        return await self._check(count=True)

    def _calls_today(self) -> int:
        today = dt_util.now().date().isoformat()
        if self._calls["day"] != today:
            self._calls = {"day": today, "count": 0}
        return self._calls["count"]

    def _over_limit(self) -> bool:
        """The daily limit stops automatic checks; Check now still works. Sends one notice per day."""
        limit = self.option(CONF_DAILY_LIMIT)
        if not limit or self._calls_today() < limit:
            return False
        if self._limit_noticed != self._calls["day"]:
            self._limit_noticed = self._calls["day"]
            self.config_entry.async_create_task(
                self.hass, async_send_limit_reached(self, limit), f"{DOMAIN} daily limit notice"
            )
        return True

    def _end_print(self, report: bool = False) -> None:
        if report and self._print_id is not None and self.option(CONF_PRINT_REPORT):
            status = self.hass.states.get(self.config_entry.data[CONF_PRINT_STATUS])
            self.config_entry.async_create_task(
                self.hass,
                async_send_print_report(self, self._print_id, status.state if status else STATE_UNAVAILABLE),
                f"{DOMAIN} print report",
            )
        self._reset()
        self._print_id = None
        self._printing_since = None
        self._earlier = []
        self._unknown_since = None
        self._errors_in_a_row = 0
        if self._down_alerted:
            self._down_alerted = False
            self.config_entry.async_create_task(
                self.hass, async_send_monitoring_restored(self, print_ended=True), f"{DOMAIN} monitoring restored"
            )

    async def async_shutdown(self) -> None:
        self._closed = True
        await self._store.async_save(self.saved_state())  # restarts and option reloads keep the alert
        await super().async_shutdown()

    @property
    def calls_today(self) -> int:
        return self._calls_today()

    @property
    def snapshot(self) -> Snapshot:
        """What the entities currently show; restored state before the first check."""
        return self._last

    async def async_check_now(self) -> Snapshot:
        """Run one check right away; it only counts toward the window during a print and outside a snooze."""
        count = (
            self.option(CONF_ENABLED)
            and self._print_state() == PRINTING
            and not self._snoozed()
            and self._stage_ready()
        )
        snapshot = await self._check(count=count)
        self._showing_manual_check = self._print_id is None
        self.async_set_updated_data(snapshot)
        return snapshot

    async def async_check_camera(self, camera: str) -> dict:
        """Judge another camera's frame once with this printer's settings, without touching its state or history."""
        try:
            answers, _usage, _frame, _type = await self._ask(camera, crop=None, earlier=None)
        except Exception as err:  # noqa: BLE001  returned to the caller
            return {"result": RESULT_ERROR, "answers": {}, "error": f"{type(err).__name__}: {err}"}
        return {"result": self._result(answers)[0], "answers": answers, "error": None}

    async def _ask(
        self, camera: str, crop: list[float] | None, earlier: tuple[bytes, str, int] | None
    ) -> tuple[dict[str, float], dict, bytes, str]:
        image = await async_get_image(self.hass, camera)
        frame, frame_type = await self.hass.async_add_executor_job(
            downscale, image.content, image.content_type, self.option(CONF_MAX_IMAGE_SIZE), crop
        )
        questions = {**self.questions, **COMPARE} if earlier else self.questions
        answers, usage = await self.async_ask_image(frame, frame_type, questions, earlier)
        return answers, usage, frame, frame_type

    async def async_ask_image(
        self,
        image: bytes,
        content_type: str,
        questions: dict[str, dict],
        earlier: tuple[bytes, str, int] | None = None,
    ) -> tuple[dict[str, float], dict]:
        """One model call with this printer's provider settings. Counts toward the daily limit."""
        data = self.config_entry.data
        self._calls_today()
        self._calls["count"] += 1
        return await ask(
            async_get_clientsession(self.hass),
            provider=data[CONF_PROVIDER],
            base_url=self.option(CONF_BASE_URL) or PROVIDERS[data[CONF_PROVIDER]][0],
            api_key=data[CONF_API_KEY],
            model=self.model,
            questions=questions,
            camera_description=self.option(CONF_CAMERA_DESCRIPTION),
            image=image,
            content_type=content_type,
            timeout=self.option(CONF_TIMEOUT),
            earlier=earlier,
        )

    def _layer(self) -> int | None:
        layer = self.config_entry.data.get(CONF_LAYER)
        state = self.hass.states.get(layer) if layer else None
        try:
            return int(float(state.state)) if state else None
        except ValueError:
            return None

    def _earlier_frame(self, layer: int | None) -> tuple[bytes, str, int] | None:
        """The newest kept frame at least compare_layers below the current layer, if any."""
        gap = self.option(CONF_COMPARE_LAYERS)
        if layer is None or gap <= 0:
            return None
        older = [e for e in self._earlier if e[0] <= layer - gap]
        if not older:
            return None
        at, frame, frame_type = older[-1]
        return frame, frame_type, layer - at

    def _keep_for_compare(self, layer: int | None, frame: bytes, frame_type: str) -> None:
        """Remember one frame per layer, dropping those older than the one the next comparison would use."""
        if layer is None or (self._earlier and self._earlier[-1][0] >= layer):
            return
        self._earlier.append((layer, frame, frame_type))
        older = [i for i, e in enumerate(self._earlier) if e[0] <= layer - self.option(CONF_COMPARE_LAYERS)]
        if older:
            del self._earlier[: older[-1]]

    def _result(self, answers: dict[str, float]) -> tuple[str, bool | None]:
        return judge(answers, self.option(CONF_YES), self.option(CONF_NO))

    async def _check(self, count: bool) -> Snapshot:
        # Taken before waiting: a snooze or dismissal while this check waits must stop it counting.
        generation = self._generation
        # One check at a time, so overlapping checks can't alert with each other's frames.
        async with self._check_lock:
            return await self._check_locked(count and generation == self._generation, generation)

    async def _check_locked(self, count: bool, generation: int) -> Snapshot:
        layer = self._layer()
        try:
            answers, usage, frame, frame_type = await self._ask(
                self.config_entry.data[CONF_CAMERA], crop=self.option(CONF_CROP), earlier=self._earlier_frame(layer)
            )
        except Exception as err:  # noqa: BLE001  shown as an error, never as "no failure"
            if self._closed:
                return self._last
            _LOGGER.warning("Check failed: %s", err)
            if isinstance(err, AuthError):
                self.config_entry.async_start_reauth(self.hass)  # asks for a new key; one flow at a time
            count = count and generation == self._generation
            now, error = dt_util.utcnow(), f"{type(err).__name__}: {err}"
            await self._record(
                at=now, result=RESULT_ERROR, answers={}, cost=None, counted=count, frame=None, error=error
            )
            if count:
                self._errors_in_a_row += 1
                if self._errors_in_a_row >= ERRORS_BEFORE_DOWN_ALERT and not self._down_alerted:
                    self._down_alerted = True
                    self._down_error = error
                    await async_send_monitoring_down(self, self._errors_in_a_row, error)
            return self._snapshot(RESULT_ERROR, error=error, checked_at=now)

        if self._closed:  # unloaded while waiting on the model
            return self._last
        if generation != self._generation:  # snoozed, dismissed or the print ended meanwhile
            count = False
        if count:
            self._errors_in_a_row = 0
            if self._down_alerted:
                self._down_alerted = False
                await async_send_monitoring_restored(self, print_ended=False)
        now = dt_util.utcnow()
        cost = _cost(usage)
        result, result_verdict = self._result(answers)
        _LOGGER.debug("Answers %s -> %s", answers, result)
        if count and answers[VISIBLE] >= self.option(CONF_YES):
            self._keep_for_compare(layer, frame, frame_type)

        if count and result_verdict is not None:
            self._verdicts.append(result_verdict)
        # Only a failed frame raises the alert, even if a lowered requirement is already met by older frames.
        alert = (
            count and result_verdict is True and not self._alerted and sum(self._verdicts) >= self.option(CONF_REQUIRED)
        )
        if alert:
            self._alerted = True
        record = await self._record(at=now, result=result, answers=answers, cost=cost, counted=count, frame=frame)
        if alert:
            await self._respond_to_failure(answers, record)

        return self._snapshot(
            result,
            answers=answers,
            checked_at=now,
            error=None,
            cost=cost,
            frame=frame,
            frame_type=frame_type,
            frame_at=now,
        )

    async def _respond_to_failure(self, answers: dict[str, float], record: dict | None) -> None:
        pause = self.config_entry.data.get(CONF_PAUSE_BUTTON)
        paused = False
        if self.option(CONF_AUTO_PAUSE) and pause:
            paused = await self._press(pause)
            self._paused_by_us = paused
        await self._record_event("alert", paused=paused)
        frame_id = record["id"] if record and record.get("frame") else None
        links = frame_links(self.hass, self.config_entry, frame_id)
        entry = self.config_entry
        self.hass.bus.async_fire(
            EVENT_FAILURE,
            {
                "entry_id": entry.entry_id,
                "printer": entry.title,
                "camera": entry.data[CONF_CAMERA],
                "paused": paused,
                "answers": answers,
                "failures": {q: answers[q] for q in failure_ids(answers) if answers[q] > self.option(CONF_YES)},
                "frame_id": frame_id,
                "frame_url": links.signed or links.path,
                "frame_path": links.path,
                "panel_url": links.panel,
            },
        )
        await async_send_failure_notification(self, answers, paused=paused, links=links)

    async def _record(self, **check) -> dict | None:
        try:
            return await self.history.async_add_check(
                **check, model=self.model, print_id=self._print_id, save=self.option(CONF_SAVE_FRAMES)
            )
        except OSError as err:  # a full or read-only disk must not stop monitoring
            _LOGGER.error("Saving frame history failed: %s", err)
            return None

    async def _record_event(self, kind: str, **data) -> None:
        try:
            await self.history.async_add_event(kind, dt_util.utcnow(), print_id=self._print_id, **data)
        except OSError as err:
            _LOGGER.error("Saving frame history failed: %s", err)

    async def _press(self, entity_id: str | None) -> bool:
        if not entity_id:
            return False
        state = self.hass.states.get(entity_id)
        if state is None or state.state == STATE_UNAVAILABLE:
            # button.press doesn't fail for a missing or unavailable entity, so check first.
            _LOGGER.error("Can't press %s: it's %s", entity_id, "missing" if state is None else "unavailable")
            return False
        try:
            await self.hass.services.async_call("button", "press", {"entity_id": entity_id}, blocking=True)
        except Exception as err:  # noqa: BLE001  report and keep monitoring
            _LOGGER.error("Pressing %s failed: %s", entity_id, err)
            return False
        return True

    def _reset(self) -> None:
        self._paused_while_alerted = False
        if self._alerted:
            self.config_entry.async_create_task(
                self.hass, async_clear_failure_notification(self), f"{DOMAIN} clear notification"
            )
        self._generation += 1
        self._verdicts.clear()
        self._alerted = False
        self._paused_by_us = False
        self._snoozed_until = None

    async def async_handle_action(self, action: str) -> None:
        """React to a notification button."""
        if not self._alerted:  # a button on an old notification, after the alert was dismissed or the print ended
            _LOGGER.info("Ignoring %s: there's no current failure alert", action)
            return
        data = self.config_entry.data
        await self._record_event("action", action=action)
        if action == ACTION_PAUSE:
            self._paused_by_us = await self._press(data.get(CONF_PAUSE_BUTTON))
        elif action == ACTION_STOP:
            await self._press(data.get(CONF_STOP_BUTTON))
        elif action in (ACTION_RESUME, ACTION_CONTINUE):
            if self._paused_by_us or action == ACTION_RESUME:
                await self._press(data.get(CONF_RESUME_BUTTON))
            self._snooze(self.option(CONF_SNOOZE))
        self.async_set_updated_data(self._snapshot(RESULT_SNOOZED if self._snoozed() else self._last.result))

    def _snooze(self, minutes: int) -> None:
        """Treat the current print as fine: clear the alert and skip checks for a while."""
        self._reset()
        self._snoozed_until = dt_util.utcnow() + timedelta(minutes=minutes)

    async def async_snooze(self, minutes: int) -> None:
        """Snooze for some minutes, or end a snooze and check again right away with 0."""
        if minutes:
            self._snooze(minutes)
            self.async_set_updated_data(self._snapshot(RESULT_SNOOZED))
        else:
            self._snoozed_until = None
            await self.async_refresh()

    async def async_set_option(self, key: str, value) -> None:
        """Change one option without reloading (used by the switches)."""
        self.hass.config_entries.async_update_entry(
            self.config_entry, options={**self.config_entry.options, key: value}
        )
        if key == CONF_CROP:
            self._earlier = []  # frames cropped differently can't be compared
        if key == CONF_ENABLED and not value:
            self._end_print()
            self.async_set_updated_data(self._snapshot(RESULT_IDLE))
        else:
            self.async_update_listeners()
