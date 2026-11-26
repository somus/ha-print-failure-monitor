"""Frame history: saved frames plus a JSON-lines index of every check and alert, with cleanup.

Layout: <config>/print_failure_monitor/<entry_id>/index.jsonl and <frame id>.jpg
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SAVE_ALL = "all"
SAVE_NOT_CLEAN = "not_clean"
SAVE_OFF = "off"
SAVE_MODES = [SAVE_ALL, SAVE_NOT_CLEAN, SAVE_OFF]

INDEX = "index.jsonl"
# Your verdict per check, set in the panel: what the frame really showed. Labeled checks are kept by
# cleanup, so they build up into a test set for tuning questions and thresholds.
LABELS = "labels.json"
LABEL_VALUES = ("failure", "fine")


def history_dir(hass: HomeAssistant, entry_id: str) -> Path:
    return Path(hass.config.path(DOMAIN, entry_id))


class FrameHistory:
    """In-memory list of records backed by an append-only index; frames stored next to it."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.dir = history_dir(hass, entry_id)
        self.records: list[dict] = []
        self.labels: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._last_id = 0

    # --- loading and writing -------------------------------------------------------------------

    async def async_load(self) -> None:
        try:
            self.records, self.labels = await self.hass.async_add_executor_job(self._load)
        except OSError as err:  # monitoring still works; checks just won't be kept
            _LOGGER.error("Loading frame history from %s failed: %s", self.dir, err)
            self.records, self.labels = [], {}
        if self.records:
            self._last_id = max(int(r["id"]) for r in self.records)

    def _load(self) -> tuple[list[dict], dict[str, str]]:
        self.dir.mkdir(parents=True, exist_ok=True)
        records = []
        path = self.dir / INDEX
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    _LOGGER.warning("Skipping a corrupt line in %s", path)
        labels = {}
        if (self.dir / LABELS).exists():
            try:
                labels = json.loads((self.dir / LABELS).read_text())
            except json.JSONDecodeError:
                _LOGGER.warning("Ignoring a corrupt %s", self.dir / LABELS)
        return records, labels

    def _next_id(self, at: datetime) -> str:
        self._last_id = max(int(at.timestamp() * 1000), self._last_id + 1)
        return str(self._last_id)

    async def _append(self, record: dict, frame: bytes | None) -> None:
        async with self._lock:
            await self.hass.async_add_executor_job(self._write, record, frame)
            self.records.append(record)

    def _write(self, record: dict, frame: bytes | None) -> None:
        jpg = self.dir / f"{record['id']}.jpg"
        try:
            if frame is not None:
                jpg.write_bytes(frame)
            with (self.dir / INDEX).open("a") as index:
                index.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError:
            jpg.unlink(missing_ok=True)  # a frame without a record would never be cleaned up
            raise

    async def async_add_check(
        self,
        *,
        at: datetime,
        result: str,
        answers: dict[str, float],
        cost: float | None,
        model: str,
        print_id: str | None,
        counted: bool,
        frame: bytes | None,
        save: str,
        error: str | None = None,
    ) -> dict:
        keep = frame is not None and (save == SAVE_ALL or (save == SAVE_NOT_CLEAN and result != "clean"))
        record = {
            "id": self._next_id(at),
            "kind": "check",
            "at": at.isoformat(),
            "result": result,
            "answers": {k: round(v, 4) for k, v in answers.items()},
            "cost": cost,
            "model": model,
            "print_id": print_id,
            "counted": counted,
            "frame": keep,
        }
        if error:
            record["error"] = error[:300]
        await self._append(record, frame if keep else None)
        return record

    async def async_add_report(self, at: datetime, print_id: str, image: bytes) -> dict:
        """The frame strip sent with a print report; kept and cleaned up like a saved frame."""
        record = {"id": self._next_id(at), "kind": "report", "at": at.isoformat(), "print_id": print_id, "frame": True}
        await self._append(record, image)
        return record

    async def async_set_label(self, frame_id: str, label: str | None) -> None:
        """Record what a checked frame really showed, or clear it with None."""
        async with self._lock:
            labels = (
                {**self.labels, frame_id: label} if label else {k: v for k, v in self.labels.items() if k != frame_id}
            )
            await self.hass.async_add_executor_job(self._write_labels, labels)
            self.labels = labels

    def _write_labels(self, labels: dict[str, str]) -> None:
        tmp = self.dir / f"{LABELS}.tmp"
        tmp.write_text(json.dumps(labels, indent=0))
        tmp.replace(self.dir / LABELS)

    async def async_add_event(self, kind: str, at: datetime, **data) -> None:
        await self._append({"id": self._next_id(at), "kind": kind, "at": at.isoformat(), **data}, None)

    # --- cleanup ---------------------------------------------------------------------------------

    async def async_cleanup(self, retention_days: int, max_mb: int, now: datetime | None = None) -> None:
        """Drop records past retention, then delete the oldest frames until under the size cap.

        Labeled checks and their frames are kept either way.
        """
        cutoff = ((now or dt_util.utcnow()) - timedelta(days=retention_days)).isoformat()
        async with self._lock:
            self.records = await self.hass.async_add_executor_job(
                self._cleanup, list(self.records), cutoff, max_mb * 1024 * 1024, set(self.labels)
            )

    def _cleanup(self, records: list[dict], cutoff: str, max_bytes: int, labeled: set[str]) -> list[dict]:
        kept = [r for r in records if r["at"] >= cutoff or r["id"] in labeled]
        changed = len(kept) != len(records)
        total = sum(self._size(r["id"]) for r in kept if r.get("frame") and r["id"] not in labeled)
        for i, record in enumerate(kept):  # oldest first; the record stays for the stats, the image goes
            if total <= max_bytes:
                break
            if record.get("frame") and record["id"] not in labeled:
                total -= self._size(record["id"])
                kept[i] = {**record, "frame": False}
                changed = True
        if changed:  # rewrite the index first, so it never points at a deleted frame
            tmp = self.dir / f"{INDEX}.tmp"
            tmp.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in kept))
            tmp.replace(self.dir / INDEX)
        # Delete every frame without a record: expired, over the cap, or left behind by a failed write.
        wanted = {f"{r['id']}.jpg" for r in kept if r.get("frame")}
        for path in self.dir.glob("*.jpg"):
            if path.name not in wanted:
                path.unlink(missing_ok=True)
        return kept

    def _size(self, frame_id: str) -> int:
        try:
            return (self.dir / f"{frame_id}.jpg").stat().st_size
        except FileNotFoundError:
            return 0

    # --- reading ---------------------------------------------------------------------------------

    def frame_path(self, frame_id: str) -> Path | None:
        if not frame_id.isdigit():
            return None
        path = self.dir / f"{frame_id}.jpg"
        return path if path.is_file() else None

    async def async_storage_bytes(self) -> int:
        ids = [r["id"] for r in self.records if r.get("frame")]
        return await self.hass.async_add_executor_job(lambda: sum(self._size(i) for i in ids))


async def async_remove_history(hass: HomeAssistant, entry_id: str) -> None:
    await hass.async_add_executor_job(shutil.rmtree, history_dir(hass, entry_id), True)
