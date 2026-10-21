"""Unit tests for intercom↔camera mapping helpers."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
from types import SimpleNamespace

ROOT = pathlib.Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "ufanet"


def _install_ha_stubs() -> None:
    if "homeassistant.helpers.entity" in sys.modules:
        return

    homeassistant = types.ModuleType("homeassistant")
    helpers = types.ModuleType("homeassistant.helpers")
    entity = types.ModuleType("homeassistant.helpers.entity")

    class DeviceInfo(dict):
        def __init__(self, **kwargs):
            super().__init__(kwargs)

    entity.DeviceInfo = DeviceInfo
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.entity"] = entity


def _load(name: str, path: pathlib.Path, package: str = "ufanet"):
    spec = importlib.util.spec_from_file_location(
        f"{package}.{name}", path, submodule_search_locations=[str(PKG)]
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{package}.{name}"] = module
    # Ensure package stub exists for relative imports.
    if package not in sys.modules:
        pkg = types.ModuleType(package)
        pkg.__path__ = [str(PKG)]
        sys.modules[package] = pkg
    spec.loader.exec_module(module)
    return module


_install_ha_stubs()
# Load const first (no HA deps beyond stubs), then api (needs aiohttp — stub), helpers.
aiohttp = types.ModuleType("aiohttp")

class ClientResponseError(Exception):
    def __init__(self, *args, **kwargs):
        self.status = kwargs.get("status", 0)
        super().__init__(*args)

class ClientSession:
    pass

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
    intercoms = [
        IntercomInfo(id=1, string_view="A", cctv_number="100"),
        IntercomInfo(id=2, string_view="B", cctv_number=None),
        IntercomInfo(id=3, string_view="C", cctv_number="200"),
    ]
    assert default_camera_map(intercoms) == {"1": "100", "3": "200"}


def test_resolve_camera_map_options_override_and_clear() -> None:
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


def test_camera_and_device_helpers() -> None:
    cam = CameraInfo(
        number="42",
        title="Entrance",
        address=None,
        domain="x.example",
        token_l="t",
    )
    assert camera_label(cam) == "Entrance (42)"
    assert cameras_by_number([cam])["42"] is cam
    assert intercom_device_id("contract", 7) == "contract_7"

    intercom = IntercomInfo(id=7, string_view="Gate", address="Street 1")
    info = intercom_device_info("contract", intercom)
    assert ("ufanet", "contract_7") in info["identifiers"]
    assert info["name"] == "Gate"
    assert info["via_device"] == ("ufanet", "contract")
    assert info["suggested_area"] == "Street 1"


if __name__ == "__main__":
    test_default_camera_map_from_cctv_number()
    test_resolve_camera_map_options_override_and_clear()
    test_camera_and_device_helpers()
    print("ok")
