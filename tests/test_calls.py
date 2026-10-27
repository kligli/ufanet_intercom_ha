"""Unit tests for doorbell call history."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load_calls() -> ModuleType:
    """Load calls.py without importing the Home Assistant package."""
    path = ROOT / "custom_components" / "ufanet" / "calls.py"
    spec = importlib.util.spec_from_file_location("ufanet_calls_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


calls = _load_calls()


def test_panel_name_prefers_street_over_role() -> None:
    """A generic role must not hide the street address."""
    assert calls.panel_name(None, "Мушникова 13/7", None, "Intercom", 5) == (
        "Мушникова 13/7"
    )


def test_visit_notice_shows_the_door_picture_without_sip() -> None:
    """A history ring still shows the snapshot when the monitor did not answer."""
    text = calls.visit_notice(
        "Мушникова 13/7",
        "camera.door",
        "button.door",
        sip_answered=False,
    )
    assert "![Дверь](/api/camera_proxy/camera.door)" in text
    assert "Мушникова 13/7" in text
    assert "Трубка не снята" in text


def test_visit_keeps_every_panel_until_history_names_one() -> None:
    """A SIP invite does not say which door rang."""
    panels = [
        (1, "111", "Мушникова 13/7"),
        (2, "222", "Другой подъезд"),
    ]
    assert calls.visit_panel_ids(panels, []) == [1, 2]


def test_visit_narrows_to_the_camera_that_rang() -> None:
    """Call history selects the panel whose camera matches."""
    moment = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    fresh = [
        calls.CallRecord(
            uuid="abc",
            called_at=moment,
            camera_number="222",
            address="Другой подъезд",
        )
    ]
    panels = [
        (1, "111", "Мушникова 13/7"),
        (2, "222", "Другой подъезд"),
    ]
    assert calls.visit_panel_ids(panels, fresh) == [2]


def test_same_street_keeps_each_house_with_its_camera() -> None:
    """13/7 must not take the camera of 21/3 just because the street matches."""
    linked = calls.link_cameras(
        [
            {"id": 1, "cctv_number": None, "label": "Мушникова, 13/7, п.1"},
            {"id": 2, "cctv_number": None, "label": "Мушникова, 21/3, п.3"},
        ],
        [
            {"number": "a", "label": "Мушникова 21/3"},
            {"number": "b", "label": "Мушникова 13/7"},
        ],
    )
    assert linked == {1: "b", 2: "a"}


def test_camera_list_puts_this_house_first() -> None:
    """A taken camera of another house sinks below the matching building."""
    ordered = calls.order_cameras_for_address(
        "Мушникова, 13/7, п.1",
        [
            {"number": "a", "label": "Мушникова 21/3"},
            {"number": "b", "label": "Мушникова 13/7"},
        ],
        {"a"},
    )
    assert [number for number, _label in ordered] == ["b", "a"]


def test_single_camera_links_to_the_only_panel() -> None:
    """One panel and one camera are the same door."""
    linked = calls.link_cameras(
        [{"id": 9, "cctv_number": None, "label": "Мушникова 13/7"}],
        [{"number": "555", "label": "Камера"}],
    )
    assert linked == {9: "555"}


def test_history_poll_stays_short_enough_for_a_doorbell() -> None:
    """A ring from call history should show up within a few seconds."""
    assert timedelta(seconds=5) == calls.CALL_HISTORY_INTERVAL


def test_naive_called_at_uses_the_panel_timezone() -> None:
    """Wall time without an offset belongs to the panel, not UTC."""
    moment = calls.parse_called_at("2026-10-25T18:06:00", "Asia/Yekaterinburg")
    assert moment == datetime(2026, 10, 25, 13, 6, tzinfo=UTC)


def test_history_row_reads_numeric_panel_offset() -> None:
    """A +05:00 timezone shifts a naive called_at back to UTC."""
    rows = calls.parse_call_history(
        {
            "results": [
                {
                    "uuid": "u",
                    "camera_number": "55",
                    "called_at": "2026-10-25T18:06:00",
                    "timezone": "+05:00",
                }
            ]
        }
    )
    assert rows[0].called_at == datetime(2026, 10, 25, 13, 6, tzinfo=UTC)


def test_call_older_than_the_ring_window_does_not_ring() -> None:
    """A late history row updates the last call and leaves the bell idle."""
    now = datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    seeded = calls.apply_history_update(calls.HistorySnapshot(), [], now)
    updated = calls.apply_history_update(
        seeded,
        [
            calls.CallRecord(
                uuid="late",
                called_at=now - timedelta(seconds=90),
                camera_number="55",
            )
        ],
        now,
    )
    assert updated.fresh == []
    assert updated.active_rings == {}
    assert updated.last_calls["55"].uuid == "late"


def test_new_call_rings_and_startup_history_does_not() -> None:
    """The first poll is silent. A later new uuid rings."""
    now = datetime(2026, 10, 24, 13, 0, tzinfo=UTC)
    seeded = calls.apply_history_update(
        calls.HistorySnapshot(),
        [
            calls.CallRecord(
                uuid="old",
                called_at=now - timedelta(minutes=30),
                camera_number="55",
            )
        ],
        now,
    )
    assert seeded.fresh == []
    ringing = calls.apply_history_update(
        seeded,
        [
            calls.CallRecord(
                uuid="new",
                called_at=now - timedelta(seconds=5),
                camera_number="55",
            )
        ],
        now,
    )
    assert [item.uuid for item in ringing.fresh] == ["new"]
    assert "55" in ringing.active_rings
