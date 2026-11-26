"""The panel's websocket API and frame endpoint, with the integration running."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.camera import Image
from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from custom_components.print_failure_monitor.panel import summarize

COORDINATOR = "custom_components.print_failure_monitor.coordinator"
FAILED = {"plate_visible": 0.9, "spaghetti": 0.92, "detached": 0.2, "nozzle_blob": 0.1}
CLEAN = {"plate_visible": 0.9, "spaghetti": 0.05, "detached": 0.1, "nozzle_blob": 0.1}


@pytest.fixture
async def running(hass, make_entry, config_dir, jpeg):
    """A loaded entry that has done 3 clean checks then 3 failed ones (alerting once)."""
    hass.states.async_set("sensor.p1s_print_status", "running")
    entry = make_entry()
    entry.add_to_hass(hass)
    with (
        patch(f"{COORDINATOR}.async_get_image", AsyncMock(return_value=Image("image/jpeg", jpeg))),
        patch(f"{COORDINATOR}.ask", new_callable=AsyncMock) as ask,
    ):
        ask.return_value = (CLEAN, {"cost": 0.0002})
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        for _ in range(2):
            await coordinator.async_refresh()
        ask.return_value = (FAILED, {"cost": 0.0002})
        for _ in range(3):
            await coordinator.async_refresh()
        await hass.async_block_till_done()
    return entry


async def test_panel_registered(hass, running, hass_client):
    assert "print-failure-monitor" in hass.data["frontend_panels"]
    resp = await (await hass_client()).get("/print_failure_monitor_static/panel.js")
    assert resp.status == 200
    assert "print-failure-monitor-panel" in await resp.text()


async def test_entries(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "print_failure_monitor/entries"})
    msg = await client.receive_json()
    assert msg["result"] == [
        {"entry_id": running.entry_id, "title": "P1S", "retention_days": 7, "camera": "camera.p1s_camera", "crop": None}
    ]


async def test_summary(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "print_failure_monitor/summary", "entry_id": running.entry_id, "hours": 24})
    result = (await client.receive_json())["result"]
    stats = result["stats"]
    assert stats["checks"] == 6
    assert stats["by_result"]["clean"] == 3 and stats["by_result"]["failed"] == 3
    assert stats["alerts"] == 1 and stats["prints"] == 1
    assert stats["cost"] == pytest.approx(0.0012)
    assert stats["storage_bytes"] > 0
    assert [p["p"] for p in result["series"]] == [0.1, 0.1, 0.1, 0.92, 0.92, 0.92]
    assert sum(d["failed"] + d["clean"] for d in result["daily"]) == 6
    assert result["yes_threshold"] == 0.7


async def test_frames_filter_page_and_image(hass, running, hass_ws_client, hass_client_no_auth, jpeg):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "print_failure_monitor/frames",
            "entry_id": running.entry_id,
            "hours": 24,
            "results": ["failed"],
            "limit": 2,
        }
    )
    page = (await client.receive_json())["result"]
    assert [f["result"] for f in page["frames"]] == ["failed", "failed"] and page["more"] is True
    assert page["frames"][0]["top"] == 0.92

    await client.send_json_auto_id(
        {
            "type": "print_failure_monitor/frames",
            "entry_id": running.entry_id,
            "hours": 24,
            "results": ["failed"],
            "before": page["frames"][-1]["id"],
        }
    )
    rest = (await client.receive_json())["result"]
    assert len(rest["frames"]) == 1 and rest["more"] is False

    http = await hass_client_no_auth()
    signed = page["frames"][0]["url"]
    resp = await http.get(signed)
    assert resp.status == 200 and await resp.read() == jpeg
    unsigned = signed.split("?")[0]
    assert (await http.get(unsigned)).status == 401


async def test_unknown_entry(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "print_failure_monitor/summary", "entry_id": "nope", "hours": 24})
    msg = await client.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "not_found"


async def test_alert_and_dismiss_counted(hass, running, hass_ws_client):
    hass.bus.async_fire("mobile_app_notification_action", {"action": f"PRINT_FAILURE_{running.entry_id}_CONTINUE"})
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "print_failure_monitor/summary", "entry_id": running.entry_id, "hours": 24})
    assert (await client.receive_json())["result"]["stats"]["dismissed"] == 1


def test_series_downsampling_keeps_spikes():
    now = dt_util.utcnow()
    records = [
        {
            "kind": "check",
            "at": (now - timedelta(seconds=30 * (2000 - i))).isoformat(),
            "result": "clean",
            "answers": {"plate_visible": 0.9, "spaghetti": 0.99 if i == 1234 else 0.05},
        }
        for i in range(2000)
    ]
    series = summarize(records, now - timedelta(days=1))["series"]
    assert len(series) <= 600
    assert max(p["p"] for p in series) == 0.99


async def test_single_frame_lookup(hass, running, hass_ws_client):
    """What the panel calls when opened from a notification."""
    frame_id = [r for r in running.runtime_data.history.records if r["kind"] == "check"][-1]["id"]
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "print_failure_monitor/frame", "entry_id": running.entry_id, "frame_id": frame_id}
    )
    frame = (await client.receive_json())["result"]
    assert frame["id"] == frame_id and frame["result"] == "failed" and "authSig=" in frame["url"]

    await client.send_json_auto_id(
        {"type": "print_failure_monitor/frame", "entry_id": running.entry_id, "frame_id": "1"}
    )
    msg = await client.receive_json()
    assert msg["success"] is False and msg["error"]["code"] == "not_found"


def test_summary_cost_unknown_when_not_reported():
    now = dt_util.utcnow()
    records = [{"id": "1", "kind": "check", "at": now.isoformat(), "result": "clean", "answers": {}, "cost": None}]
    assert summarize(records, now - timedelta(hours=1))["stats"]["cost"] is None


async def test_frames_rejects_bad_paging_id(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "print_failure_monitor/frames", "entry_id": running.entry_id, "hours": 24, "before": "abc"}
    )
    msg = await client.receive_json()
    assert not msg["success"] and msg["error"]["code"] == "invalid_format"


async def _ws(client, **msg):
    await client.send_json_auto_id(msg)
    return await client.receive_json()


async def test_labels(hass, running, hass_ws_client):
    """Label a false alarm and a caught failure, see them in frames and stats, then clear one."""
    client = await hass_ws_client(hass)
    entry = running.entry_id
    frames = (await _ws(client, type="print_failure_monitor/frames", entry_id=entry, hours=24))["result"]["frames"]
    failed = next(f for f in frames if f["result"] == "failed")
    clean = next(f for f in frames if f["result"] == "clean")
    assert failed["label"] is None

    for frame, label in ((failed, "fine"), (clean, "failure")):  # a false alarm and a miss
        msg = await _ws(client, type="print_failure_monitor/label", entry_id=entry, frame_id=frame["id"], label=label)
        assert msg["result"] == {"label": label}

    labeled = (await _ws(client, type="print_failure_monitor/frames", entry_id=entry, hours=24, labeled=True))["result"]
    assert {(f["id"], f["label"]) for f in labeled["frames"]} == {(failed["id"], "fine"), (clean["id"], "failure")}
    stats = (await _ws(client, type="print_failure_monitor/summary", entry_id=entry, hours=24))["result"]["stats"]
    assert stats["labels"] == {"total": 2, "false_alarms": 1, "missed": 1}

    await _ws(client, type="print_failure_monitor/label", entry_id=entry, frame_id=clean["id"], label=None)
    stats = (await _ws(client, type="print_failure_monitor/summary", entry_id=entry, hours=24))["result"]["stats"]
    assert stats["labels"] == {"total": 1, "false_alarms": 1, "missed": 0}

    bad = await _ws(client, type="print_failure_monitor/label", entry_id=entry, frame_id=failed["id"], label="maybe")
    gone = await _ws(client, type="print_failure_monitor/label", entry_id=entry, frame_id="123", label="fine")
    assert bad["error"]["code"] == "invalid_format" and gone["error"]["code"] == "not_found"


async def test_set_crop(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    entry = running.entry_id
    msg = await _ws(client, type="print_failure_monitor/set_crop", entry_id=entry, crop=[0.1, 0.2, 0.9, 1])
    assert msg["result"] == {"crop": [0.1, 0.2, 0.9, 1.0]}
    assert running.options["crop"] == [0.1, 0.2, 0.9, 1.0]
    assert (await _ws(client, type="print_failure_monitor/entries"))["result"][0]["crop"] == [0.1, 0.2, 0.9, 1.0]

    for bad in ([0.5, 0, 0.55, 1], [0, 0, 1.2, 1], [0.9, 0, 0.1, 1], [0, 0, 1], [0, 0, 1, 0.15]):  # last: a thin strip
        msg = await _ws(client, type="print_failure_monitor/set_crop", entry_id=entry, crop=bad)
        assert not msg["success"], bad
    await _ws(client, type="print_failure_monitor/set_crop", entry_id=entry, crop=None)
    assert running.options["crop"] is None


async def _label_false_alarm(client, entry):
    """Label one failed frame as fine and one clean frame as fine; returns the frames."""
    frames = (await _ws(client, type="print_failure_monitor/frames", entry_id=entry, hours=24))["result"]["frames"]
    failed = next(f for f in frames if f["result"] == "failed")
    clean = next(f for f in frames if f["result"] == "clean")
    for frame in (failed, clean):
        await _ws(client, type="print_failure_monitor/label", entry_id=entry, frame_id=frame["id"], label="fine")
    return failed, clean


async def test_try_thresholds_on_labeled_frames(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    entry = running.entry_id
    await _label_false_alarm(client, entry)
    settings = (await _ws(client, type="print_failure_monitor/settings", entry_id=entry))["result"]
    assert settings == {"yes": 0.7, "no": 0.3, "questions": "", "labeled": 2, "with_frames": 2}

    with patch(f"{COORDINATOR}.ask", new_callable=AsyncMock) as ask:
        msg = await _ws(
            client, type="print_failure_monitor/try_settings", entry_id=entry, yes=0.95, no=0.3, questions=""
        )
        ask.assert_not_called()  # same questions: the stored answers are reused
    result = msg["result"]
    assert result["asked"] == 0
    assert result["current"] == {"false_alarms": 1, "missed": 0}
    assert result["proposed"] == {"false_alarms": 0, "missed": 0}
    # 0.95 also gates plate_visible (0.9), so both frames are skipped as not visible
    assert sorted((r["before"], r["after"]) for r in result["rows"]) == [
        ("clean", "not_visible"),
        ("failed", "not_visible"),
    ]

    bad = await _ws(client, type="print_failure_monitor/try_settings", entry_id=entry, yes=0.5, no=0.6, questions="")
    assert bad["error"]["code"] == "invalid"


async def test_try_new_questions_asks_again(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    entry = running.entry_id
    await _label_false_alarm(client, entry)
    with patch(f"{COORDINATOR}.ask", new_callable=AsyncMock) as ask:
        ask.return_value = ({**CLEAN, "warping": 0.9}, {})
        msg = await _ws(
            client,
            type="print_failure_monitor/try_settings",
            entry_id=entry,
            yes=0.7,
            no=0.3,
            questions="warping: true\n",
        )
    result = msg["result"]
    assert result["asked"] == 2 and ask.call_count == 2
    assert "warping" in ask.call_args.kwargs["questions"]
    assert result["proposed"] == {"false_alarms": 2, "missed": 0}  # warping fires on both

    bad = await _ws(
        client, type="print_failure_monitor/try_settings", entry_id=entry, yes=0.7, no=0.3, questions="nope: true"
    )
    assert "isn't an optional question" in bad["error"]["message"]


async def test_save_settings(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    msg = await _ws(
        client,
        type="print_failure_monitor/save_settings",
        entry_id=running.entry_id,
        yes=0.8,
        no=0.25,
        questions="warping: true",
    )
    assert msg["success"]
    await hass.async_block_till_done()
    assert running.options["yes_threshold"] == 0.8 and running.options["questions"] == {"warping": True}
    assert "warping" in running.runtime_data.questions  # reloaded with the new questions


@pytest.mark.parametrize("text", ["# warping: true", "~", "[]", "false"])
async def test_settings_that_mean_no_overrides_are_saved_as_none(hass, running, hass_ws_client, text):
    client = await hass_ws_client(hass)
    tried = await _ws(
        client, type="print_failure_monitor/try_settings", entry_id=running.entry_id, yes=0.7, no=0.3, questions=text
    )
    assert tried["success"]
    saved = await _ws(
        client, type="print_failure_monitor/save_settings", entry_id=running.entry_id, yes=0.7, no=0.3, questions=text
    )
    assert saved["success"]
    await hass.async_block_till_done(wait_background_tasks=True)
    assert running.options["questions"] == {}
    assert running.state is ConfigEntryState.LOADED


async def test_settings_yaml_doesnt_resolve_tags(hass, running, hass_ws_client, monkeypatch):
    monkeypatch.setenv("PFM_TEST_SECRET", "leak")
    client = await hass_ws_client(hass)
    msg = await _ws(
        client,
        type="print_failure_monitor/try_settings",
        entry_id=running.entry_id,
        yes=0.7,
        no=0.3,
        questions="spaghetti:\n  instructions: !env_var PFM_TEST_SECRET\n",
    )
    assert msg["error"]["code"] == "invalid" and "leak" not in msg["error"]["message"]


async def test_retest_keeps_the_stored_comparison_answer(hass, running, hass_ws_client):
    client = await hass_ws_client(hass)
    coordinator = running.runtime_data
    record = next(r for r in reversed(coordinator.history.records) if r["kind"] == "check")
    record["answers"] = {**CLEAN, "not_growing": 0.9}  # judged failed on the comparison alone
    await coordinator.history.async_set_label(record["id"], "fine")
    with patch(f"{COORDINATOR}.ask", new_callable=AsyncMock) as ask:
        ask.return_value = ({**CLEAN, "warping": 0.1}, {})
        msg = await _ws(
            client,
            type="print_failure_monitor/try_settings",
            entry_id=running.entry_id,
            yes=0.7,
            no=0.3,
            questions="warping: true",
        )
    row = next(r for r in msg["result"]["rows"] if r["id"] == record["id"])
    assert row["before"] == row["after"] == "failed"
    assert msg["result"]["current"] == msg["result"]["proposed"]


@pytest.mark.parametrize(
    "msg",
    [
        {"type": "print_failure_monitor/set_crop", "crop": None},
        {"type": "print_failure_monitor/try_settings", "yes": 0.7, "no": 0.3, "questions": ""},
        {"type": "print_failure_monitor/save_settings", "yes": 0.7, "no": 0.3, "questions": ""},
        {"type": "print_failure_monitor/label", "frame_id": "1", "label": "fine"},
    ],
)
async def test_changes_need_an_admin(hass, running, hass_ws_client, hass_read_only_access_token, msg):
    client = await hass_ws_client(hass, hass_read_only_access_token)
    result = await _ws(client, entry_id=running.entry_id, **msg)
    assert result["error"]["code"] == "unauthorized"
