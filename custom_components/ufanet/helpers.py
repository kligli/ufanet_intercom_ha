"""Shared helpers for Ufanet Intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.entity import DeviceInfo

from .api import CameraInfo, IntercomInfo, intercom_display_name
from .const import CONF_CAMERA_MAP, DOMAIN, NONE_CAMERA

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def intercom_device_id(contract: str, intercom_id: int) -> str:
    """Stable device identifier for an intercom (+ its camera)."""
    return f"{contract}_{intercom_id}"


def intercom_device_info(
    contract: str, intercom: IntercomInfo, *, via_hub: bool = True
) -> DeviceInfo:
    """DeviceInfo shared by the open-door button and linked camera."""
    kwargs: dict = {
        "identifiers": {(DOMAIN, intercom_device_id(contract, intercom.id))},
        "name": intercom_display_name(intercom),
        "manufacturer": "Ufanet",
        "model": "Intercom",
    }
    if via_hub:
        kwargs["via_device"] = (DOMAIN, contract)
    if intercom.address:
        kwargs["suggested_area"] = intercom.address
    return DeviceInfo(**kwargs)


def default_camera_map(intercoms: list[IntercomInfo]) -> dict[str, str]:
    """Build default intercom_id → camera_number from Dom API cctv_number."""
    mapping: dict[str, str] = {}
    for intercom in intercoms:
        if intercom.cctv_number:
            mapping[str(intercom.id)] = str(intercom.cctv_number)
    return mapping


def resolve_camera_map(
    entry: ConfigEntry, intercoms: list[IntercomInfo]
) -> dict[str, str]:
    """Merge API defaults with user options (options win).

    Options value ``NONE_CAMERA`` (or empty) means «no camera» for that
    intercom and clears the API default.
    """
    mapping = default_camera_map(intercoms)
    overrides = entry.options.get(CONF_CAMERA_MAP) or {}
    for intercom_id, camera_number in overrides.items():
        key = str(intercom_id)
        if camera_number in (None, "", NONE_CAMERA):
            mapping.pop(key, None)
        else:
            mapping[key] = str(camera_number)
    return mapping


def camera_label(cam: CameraInfo) -> str:
    """Human-readable label for a camera selector."""
    title = cam.title or cam.address or cam.number
    if cam.title or cam.address:
        return f"{title} ({cam.number})"
    return str(cam.number)


def cameras_by_number(cameras: list[CameraInfo]) -> dict[str, CameraInfo]:
    """Index cameras by their number string."""
    return {str(cam.number): cam for cam in cameras}
