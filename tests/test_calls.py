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


def test_single_camera_links_to_the_only_panel() -> None:
    """One panel and one camera are the same door."""
    linked = calls.link_cameras(
        [{"id": 9, "cctv_number": None, "label": "Мушникова 13/7"}],
        [{"number": "555", "label": "Камера"}],
    )
    assert linked == {9: "555"}


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
