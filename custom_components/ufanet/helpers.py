"""Shared helpers for Ufanet Intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.entity import DeviceInfo

from .api import CameraInfo, IntercomInfo, intercom_display_name
from .const import CONF_CAMERA_MAP, DOMAIN, NONE_CAMERA

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def supports_via_device_id() -> bool:
    """Return True when DeviceInfo accepts via_device_id (HA 2026.8+)."""
    optional = getattr(DeviceInfo, "__optional_keys__", None)
    if optional is not None and "via_device_id" in optional:
        return True
    annotations = getattr(DeviceInfo, "__annotations__", {})
    return "via_device_id" in annotations


def intercom_device_id(contract: str, intercom_id: int) -> str:
    """Stable device identifier for an intercom (+ its camera)."""
    return f"{contract}_{intercom_id}"


def intercom_device_info(
    contract: str,
    intercom: IntercomInfo,
    *,
    hub_device_id: str | None = None,
) -> DeviceInfo:
    """DeviceInfo shared by the open-door button and linked camera."""
    kwargs: dict = {
        "identifiers": {(DOMAIN, intercom_device_id(contract, intercom.id))},
        "name": intercom_display_name(intercom),
        "manufacturer": "Ufanet",
        "model": "Video intercom" if intercom.cctv_number else "Intercom",
    }
    # Prefer via_device_id; never set deprecated via_device (raises on modern HA).
    if hub_device_id and supports_via_device_id():
        kwargs["via_device_id"] = hub_device_id
    if intercom.address:
        kwargs["suggested_area"] = intercom.address
    return DeviceInfo(**kwargs)


def filter_enabled_intercoms(
    intercoms: list[IntercomInfo],
    enabled_ids: list[Any] | None,
    disabled_ids: list[Any] | None = None,
) -> list[IntercomInfo]:
    """Keep the addresses the user chose and drop ones they removed.

    A missing enabled list means the whole account, minus removed doors.
    An explicit enabled list is used as-is, so clearing a door stays cleared.
    """
    disabled = {str(item) for item in (disabled_ids or [])}
    if enabled_ids:
        allow = {str(item) for item in enabled_ids} - disabled
        return [intercom for intercom in intercoms if str(intercom.id) in allow]
    return [intercom for intercom in intercoms if str(intercom.id) not in disabled]


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


def live_stream_url(domain: str, number: str, token: str) -> str:
    """HTTPS live view.

    RTSP port 554 from Home Assistant to the camera times out. Flussonic
    serves the same picture and AAC audio on port 443.
    """
    return f"https://{domain}/{number}/index.m3u8?token={token}&tracks=v1a1"


def whep_url(domain: str, number: str, token: str) -> str:
    """WebRTC playback URL for the same video and microphone tracks."""
    return f"https://{domain}/{number}/whep?token={token}&tracks=v1a1"
