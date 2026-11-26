"""Frame history: what gets saved, reloading, and cleanup by age and size."""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.print_failure_monitor.history import FrameHistory, async_remove_history, history_dir


@pytest.fixture
async def history(hass: HomeAssistant, config_dir):
    h = FrameHistory(hass, "entry1")
    await h.async_load()
    return h


async def _add(history, at, result="clean", frame=b"x" * 1000, save="all"):
    await history.async_add_check(
        at=at,
        result=result,
        answers={"plate_visible": 0.9, "spaghetti": 0.1},
        cost=0.0002,
        model="m",
        print_id="p1",
        counted=True,
        frame=frame,
        save=save,
    )
    return history.records[-1]


@pytest.mark.parametrize(
    ("save", "result", "kept"),
    [("all", "clean", True), ("not_clean", "clean", False), ("not_clean", "unsure", True), ("off", "failed", False)],
)
async def test_save_modes(history, save, result, kept):
    record = await _add(history, dt_util.utcnow(), result=result, save=save)
    assert record["frame"] is kept
    assert (history.frame_path(record["id"]) is not None) is kept
    assert record["result"] == result  # the result is recorded either way


async def test_reload_from_disk(hass, history):
    now = dt_util.utcnow()
    first = await _add(history, now)
    await history.async_add_event("alert", now, paused=True)
    reloaded = FrameHistory(hass, "entry1")
    await reloaded.async_load()
    assert [r["kind"] for r in reloaded.records] == ["check", "alert"]
    assert reloaded.records[0] == first
    second = await _add(reloaded, now)  # same timestamp still gets a unique, later id
    assert int(second["id"]) > int(first["id"])


async def test_cleanup_by_age(history):
    now = dt_util.utcnow()
    old = await _add(history, now - timedelta(days=8))
    new = await _add(history, now - timedelta(days=1))
    await history.async_cleanup(retention_days=7, max_mb=1024, now=now)
    assert [r["id"] for r in history.records] == [new["id"]]
    assert history.frame_path(old["id"]) is None
    assert history.frame_path(new["id"]) is not None


async def test_cleanup_by_size_keeps_results(hass, history):
    now = dt_util.utcnow()
    big = b"x" * (600 * 1024)
    oldest = await _add(history, now - timedelta(hours=3), frame=big)
    middle = await _add(history, now - timedelta(hours=2), frame=big)
    newest = await _add(history, now - timedelta(hours=1), frame=big)
    await history.async_cleanup(retention_days=7, max_mb=1, now=now)

    assert len(history.records) == 3  # stats survive
    assert [r["frame"] for r in history.records] == [False, False, True]
    assert history.frame_path(oldest["id"]) is None and history.frame_path(middle["id"]) is None
    assert history.frame_path(newest["id"]) is not None
    assert await history.async_storage_bytes() == len(big)

    reloaded = FrameHistory(hass, "entry1")
    await reloaded.async_load()
    assert [r["frame"] for r in reloaded.records] == [False, False, True]


async def test_frame_path_rejects_traversal(history):
    await _add(history, dt_util.utcnow())
    assert history.frame_path("../index.jsonl") is None
    assert history.frame_path("index") is None


async def test_remove(hass, history):
    await _add(history, dt_util.utcnow())
    assert history_dir(hass, "entry1").exists()
    await async_remove_history(hass, "entry1")
    assert not history_dir(hass, "entry1").exists()


async def test_failed_index_write_leaves_no_frame(history):
    (history.dir / "index.jsonl").mkdir()  # any OSError on the index append
    with pytest.raises(OSError):
        await _add(history, dt_util.utcnow())
    assert list(history.dir.glob("*.jpg")) == []


async def test_cleanup_removes_frames_without_a_record(history):
    record = await _add(history, dt_util.utcnow())
    (history.dir / "123.jpg").write_bytes(b"orphan")
    await history.async_cleanup(retention_days=7, max_mb=1024)
    assert sorted(p.name for p in history.dir.glob("*.jpg")) == [f"{record['id']}.jpg"]


async def test_labels_persist_and_survive_cleanup(hass, history):
    now = dt_util.utcnow()
    old_labeled = await _add(history, now - timedelta(days=30))
    old = await _add(history, now - timedelta(days=30))
    await history.async_set_label(old_labeled["id"], "failure")

    reloaded = FrameHistory(hass, "entry1")
    await reloaded.async_load()
    assert reloaded.labels == {old_labeled["id"]: "failure"}

    await reloaded.async_cleanup(retention_days=7, max_mb=0, now=now)  # past retention and over a zero cap
    assert [r["id"] for r in reloaded.records] == [old_labeled["id"]]
    assert reloaded.frame_path(old_labeled["id"]) is not None  # the frame is kept too
    assert reloaded.frame_path(old["id"]) is None
