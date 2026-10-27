"""Unit tests for intercom↔camera mapping helpers."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from types import ModuleType, SimpleNamespace
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "ufanet"


def _install_ha_stubs(*, via_device_id: bool = True) -> None:
    """Install / refresh HA stubs used by helpers."""
    homeassistant = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    entity = types.ModuleType("homeassistant.helpers.entity")

    annotations: dict[str, type] = {
        "identifiers": set,
        "name": str,
        "manufacturer": str,
        "model": str,
        "suggested_area": str,
    }
    if via_device_id:
        annotations["via_device_id"] = str

    class DeviceInfo(dict):
        """Minimal DeviceInfo stand-in with optional annotations."""

        __annotations__ = annotations

        def __init__(self, **kwargs: Any) -> None:
            """Store kwargs as a dict."""
            super().__init__(kwargs)

    entity.DeviceInfo = DeviceInfo
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.entity"] = entity


def _load(name: str, path: pathlib.Path, package: str = "ufanet") -> ModuleType:
    """Load a package module from disk, replacing any cached copy."""
    # Drop cached module so annotation changes on DeviceInfo are picked up.
    sys.modules.pop(f"{package}.{name}", None)
    spec = importlib.util.spec_from_file_location(
        f"{package}.{name}", path, submodule_search_locations=[str(PKG)]
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package}.{name}"] = module
    # Ensure package stub exists for relative imports.
    if package not in sys.modules:
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(PKG)]
        sys.modules[package] = pkg
    spec.loader.exec_module(module)
    return module


_install_ha_stubs(via_device_id=True)
# Load const first (no HA deps beyond stubs), then api (needs aiohttp — stub), helpers.
aiohttp = types.ModuleType("aiohttp")


class ClientResponseError(Exception):
    """Stub aiohttp.ClientResponseError."""

    def __init__(self, *args: object, **kwargs: Any) -> None:
        """Capture status for callers that inspect it."""
        self.status = kwargs.get("status", 0)
        super().__init__(*args)


class ClientSession:
    """Stub aiohttp.ClientSession."""


aiohttp.ClientResponseError = ClientResponseError
aiohttp.ClientSession = ClientSession
sys.modules["aiohttp"] = aiohttp

const = _load("const", PKG / "const.py")
api = _load("api", PKG / "api.py")
helpers = _load("helpers", PKG / "helpers.py")

CameraInfo = api.CameraInfo
IntercomInfo = api.IntercomInfo
NONE_CAMERA = const.NONE_CAMERA
camera_label = helpers.camera_label
cameras_by_number = helpers.cameras_by_number
default_camera_map = helpers.default_camera_map
intercom_device_id = helpers.intercom_device_id
intercom_device_info = helpers.intercom_device_info
resolve_camera_map = helpers.resolve_camera_map


def test_default_camera_map_from_cctv_number() -> None:
    """API cctv_number becomes the default intercom→camera map."""
    intercoms = [
        IntercomInfo(id=1, string_view="A", cctv_number="100"),
        IntercomInfo(id=2, string_view="B", cctv_number=None),
        IntercomInfo(id=3, string_view="C", cctv_number="200"),
    ]
    assert default_camera_map(intercoms) == {"1": "100", "3": "200"}


def test_resolve_camera_map_options_override_and_clear() -> None:
    """Options override API defaults; NONE_CAMERA clears a link."""
    intercoms = [
        IntercomInfo(id=1, cctv_number="100"),
        IntercomInfo(id=2, cctv_number="200"),
    ]
    entry = SimpleNamespace(
        options={
            "camera_map": {
                "1": "999",
                "2": NONE_CAMERA,
            }
        }
    )
    assert resolve_camera_map(entry, intercoms) == {"1": "999"}


def test_live_stream_uses_https_instead_of_rtsp() -> None:
    """Live view stays on port 443. RTSP 554 times out from Home Assistant."""
    assert helpers.live_stream_url("cam.example", "42", "tok") == (
        "https://cam.example/42/index.m3u8?token=tok&tracks=v1a1"
    )
    assert "tracks=v1a1" in helpers.whep_url("cam.example", "42", "tok")


def test_camera_and_device_helpers() -> None:
    """Labels, indexing, and via_device_id DeviceInfo wiring."""
    cam = CameraInfo(
        number="42",
        title="Entrance",
        address=None,
        domain="x.example",
        token_l="t",  # noqa: S106
    )
    assert camera_label(cam) == "Entrance (42)"
    assert cameras_by_number([cam])["42"] is cam
    assert intercom_device_id("contract", 7) == "contract_7"

    intercom = IntercomInfo(id=7, string_view="Gate", address="Street 1")
    info = intercom_device_info("contract", intercom, hub_device_id="hub-abc")
    assert ("ufanet", "contract_7") in info["identifiers"]
    assert info["name"] == "Gate"
    assert info["via_device_id"] == "hub-abc"
    assert "via_device" not in info
    assert info["suggested_area"] == "Street 1"

    # Without hub id, omit via link entirely.
    info_no_hub = intercom_device_info("contract", intercom)
    assert "via_device_id" not in info_no_hub
    assert "via_device" not in info_no_hub


def test_intercom_device_info_omits_via_on_old_ha() -> None:
    """Older DeviceInfo without via_device_id should not use deprecated via_device."""
    _install_ha_stubs(via_device_id=False)
    old_helpers = _load("helpers", PKG / "helpers.py")
    intercom = IntercomInfo(id=7, string_view="Gate", address="Street 1")
    info = old_helpers.intercom_device_info(
        "contract", intercom, hub_device_id="hub-abc"
    )
    assert "via_device_id" not in info
    assert "via_device" not in info
    assert ("ufanet", "contract_7") in info["identifiers"]
    # Restore modern stubs for any subsequent imports.
    _install_ha_stubs(via_device_id=True)
    _load("helpers", PKG / "helpers.py")


if __name__ == "__main__":
    test_live_stream_uses_https_instead_of_rtsp()
    test_default_camera_map_from_cctv_number()
    test_resolve_camera_map_options_override_and_clear()
    test_camera_and_device_helpers()
    test_intercom_device_info_omits_via_on_old_ha()
