"""Call-history parsing for the Ufanet video intercom.

Ufanet does not push doorbell rings. The integration polls
``api/v1/skuds/call-history/`` and turns new rows into a single ring.
History already present at startup is remembered, not announced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CALL_HISTORY_INTERVAL = timedelta(seconds=5)
CALL_HISTORY_PAGE_SIZE = 25
# A row this old is backlog after downtime, not someone at the door now.
STALE_CALL_AGE = timedelta(minutes=5)
# How long the doorbell binary sensor stays on after a ring.
RING_WINDOW = timedelta(seconds=60)
# Ignore timestamps that claim the call is more than a minute in the future.
_FUTURE_SKEW = timedelta(seconds=60)
_SEEN_UUID_LIMIT = 400


@dataclass(frozen=True)
class CallRecord:
    """One doorbell call from Ufanet history."""

    uuid: str
    called_at: datetime
    camera_number: str
    address: str | None = None
    porch: str | None = None
    flat: str | None = None


@dataclass
class HistorySnapshot:
    """Ring state derived from polled call history."""

    seen: set[str] = field(default_factory=set)
    bootstrapped: bool = False
    fresh: list[CallRecord] = field(default_factory=list)
    last_calls: dict[str, CallRecord] = field(default_factory=dict)
    active_rings: dict[str, datetime] = field(default_factory=dict)


def normalize_camera_number(value: Any) -> str | None:
    """Return a camera number comparable across skud and cctv payloads."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def _as_optional_str(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


_OFFSET_TEXT = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _zone_from_name(name: Any) -> timezone | ZoneInfo | None:
    """Return a tzinfo for a panel timezone name or numeric offset."""
    text = _as_optional_str(name)
    if text is None:
        return None
    offset = _OFFSET_TEXT.fullmatch(text.strip())
    if offset:
        sign = 1 if offset.group(1) == "+" else -1
        hours = int(offset.group(2))
        minutes = int(offset.group(3) or 0)
        return timezone(sign * timedelta(hours=hours, minutes=minutes))
    try:
        return ZoneInfo(text.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def parse_called_at(value: Any, timezone_name: Any = None) -> datetime | None:
    """Parse a call timestamp into aware UTC.

    A timestamp that already carries an offset is used as-is. A naive
    timestamp is read in the panel timezone from the same history row.
    """
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds /= 1000
        moment = datetime.fromtimestamp(seconds, tz=UTC)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_zone_from_name(timezone_name) or UTC)
    return moment.astimezone(UTC)


def parse_call_history(payload: Any) -> list[CallRecord]:
    """Parse a call-history response object or a bare list."""
    if isinstance(payload, dict):
        items = payload.get("results") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    records: list[CallRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        uuid = _as_optional_str(item.get("uuid"))
        camera_number = normalize_camera_number(item.get("camera_number"))
        called_at = parse_called_at(item.get("called_at"), item.get("timezone"))
        if not uuid or not camera_number or called_at is None:
            continue
        records.append(
            CallRecord(
                uuid=uuid,
                called_at=called_at,
                camera_number=camera_number,
                address=_as_optional_str(item.get("address")),
                porch=_as_optional_str(item.get("porch")),
                flat=_as_optional_str(item.get("flat")),
            )
        )
    return records


def _merge_last_calls(
    previous: dict[str, CallRecord], calls: list[CallRecord]
) -> dict[str, CallRecord]:
    merged = dict(previous)
    for call in calls:
        current = merged.get(call.camera_number)
        if current is None or call.called_at >= current.called_at:
            merged[call.camera_number] = call
    return merged


def _trim_seen(seen: set[str], calls: list[CallRecord]) -> set[str]:
    if len(seen) <= _SEEN_UUID_LIMIT:
        return seen
    keep = {call.uuid for call in calls}
    trimmed = set(keep)
    for uuid in seen:
        if len(trimmed) >= _SEEN_UUID_LIMIT:
            break
        trimmed.add(uuid)
    return trimmed


def apply_history_update(
    previous: HistorySnapshot,
    calls: list[CallRecord],
    now: datetime,
    *,
    stale_after: timedelta = STALE_CALL_AGE,
    ring_window: timedelta = RING_WINDOW,
) -> HistorySnapshot:
    """Fold one poll into ring state.

    The first successful poll only records what already happened.
    Later polls emit ``fresh`` for calls that are new and recent.
    """
    last_calls = _merge_last_calls(previous.last_calls, calls)
    if not previous.bootstrapped:
        return HistorySnapshot(
            seen={call.uuid for call in calls},
            bootstrapped=True,
            fresh=[],
            last_calls=last_calls,
            active_rings={},
        )

    seen = set(previous.seen)
    fresh: list[CallRecord] = []
    active = {
        camera: called_at
        for camera, called_at in previous.active_rings.items()
        if now - called_at < ring_window
    }
    for call in sorted(calls, key=lambda item: item.called_at):
        if call.uuid in seen:
            continue
        seen.add(call.uuid)
        age = now - call.called_at
        if age > stale_after or age < -_FUTURE_SKEW or age > ring_window:
            continue
        fresh.append(call)
        active[call.camera_number] = call.called_at

    return HistorySnapshot(
        seen=_trim_seen(seen, calls),
        bootstrapped=True,
        fresh=fresh,
        last_calls=last_calls,
        active_rings=active,
    )


# Role names the API uses for every panel. The street address is the useful name.
_GENERIC_PANEL_NAMES = frozenset(
    {
        "intercom",
        "домофон",
        "видеодомофон",
        "video intercom",
        "скуд",
        "skud",
        "камера",
        "camera",
    }
)
_LABEL_STOPWORDS = _GENERIC_PANEL_NAMES | frozenset(
    {
        "г",  # noqa: RUF001
        "город",
        "ул",
        "улица",
        "д",
        "дом",
        "п",
        "подъезд",
        "кв",
        "квартира",
    }
)


def panel_name(
    custom_name: str | None = None,
    string_view: str | None = None,
    address: str | None = None,
    role_name: str | None = None,
    fallback_id: int | None = None,
) -> str:
    """Prefer the street address over a generic role such as Intercom."""
    for candidate in (custom_name, string_view, address, role_name):
        text = (candidate or "").strip()
        if text and text.casefold() not in _GENERIC_PANEL_NAMES:
            return text
    if fallback_id is not None:
        return f"Intercom {fallback_id}"
    return "Intercom"


def skud_camera_number(item: dict[str, Any]) -> str | None:
    """Read the panel camera id from a shared-skud row."""
    for key in ("cctv_number", "camera_number"):
        found = normalize_camera_number(item.get(key))
        if found:
            return found
    camera = item.get("camera")
    if isinstance(camera, dict):
        for key in ("number", "cctv_number", "camera_number"):
            found = normalize_camera_number(camera.get(key))
            if found:
                return found
        return None
    found = normalize_camera_number(camera)
    if found and " " not in found:
        return found
    return None


def label_tokens(text: str | None) -> set[str]:
    """Tokens that can connect a panel, a camera title and a call address."""
    if not text:
        return set()
    tokens = re.findall(r"[0-9a-zа-яё]+", text.casefold())  # noqa: RUF001
    return {
        token
        for token in tokens
        if token not in _LABEL_STOPWORDS and (len(token) >= 3 or token.isdigit())
    }


def label_score(left: str | None, right: str | None) -> int:
    """Score two addresses. A street plus a house number is a real match."""
    common = label_tokens(left) & label_tokens(right)
    if not common:
        return 0
    has_word = any(not token.isdigit() and len(token) >= 4 for token in common)
    has_number = any(token.isdigit() for token in common)
    if has_word and has_number:
        return 10 + len(common)
    if has_word:
        return 5
    return 0


def link_cameras(
    intercoms: list[dict[str, Any]], cameras: list[dict[str, Any]]
) -> dict[int, str]:
    """Map an intercom id to the camera number that shows its door.

    Exact ``cctv_number`` wins. Otherwise match the street address. A lone
    panel with a lone camera are the same door even when the ids differ.
    """
    assigned: dict[int, str] = {}
    used: set[str] = set()
    numbers = {str(camera["number"]) for camera in cameras}
    for intercom in intercoms:
        cctv = intercom.get("cctv_number")
        if cctv and cctv in numbers and cctv not in used:
            assigned[int(intercom["id"])] = cctv
            used.add(cctv)

    pairs: list[tuple[int, int, str]] = []
    for intercom in intercoms:
        if int(intercom["id"]) in assigned:
            continue
        for camera in cameras:
            number = str(camera["number"])
            if number in used:
                continue
            score = label_score(intercom.get("label"), camera.get("label"))
            if score:
                pairs.append((score, int(intercom["id"]), number))
    for _score, intercom_id, number in sorted(pairs, reverse=True):
        if intercom_id in assigned or number in used:
            continue
        assigned[intercom_id] = number
        used.add(number)

    if (
        len(intercoms) == 1
        and len(cameras) == 1
        and int(intercoms[0]["id"]) not in assigned
    ):
        assigned[int(intercoms[0]["id"])] = str(cameras[0]["number"])
    return assigned


def visit_panel_ids(
    panels: list[tuple[int, str | None, str | None]],
    calls: list[CallRecord],
) -> list[int]:
    """Return the panel ids that belong to this visit.

    ``panels`` is ``(id, camera_number, label)``. A single panel is that
    door. Several panels narrow to the call that just arrived. Until
    history names a door, every panel stays in the notification.
    """
    if not panels:
        return []
    if len(panels) == 1:
        return [panels[0][0]]
    matched = [
        panel_id
        for panel_id, camera, label in panels
        if any(call_matches_panel(call, camera, label) for call in calls)
    ]
    return matched or [panel_id for panel_id, _camera, _label in panels]


def call_matches_panel(
    call: CallRecord, camera_number: str | None, label: str | None
) -> bool:
    """Return whether a history row is a call to this panel."""
    if camera_number:
        return call.camera_number == camera_number
    return (
        label_score(
            label, " ".join(part for part in (call.address, call.porch) if part)
        )
        > 0
    )


def expire_rings(
    previous: HistorySnapshot,
    now: datetime,
    *,
    ring_window: timedelta = RING_WINDOW,
) -> HistorySnapshot:
    """Drop rings whose window has elapsed without polling again."""
    active = {
        camera: called_at
        for camera, called_at in previous.active_rings.items()
        if now - called_at < ring_window
    }
    return HistorySnapshot(
        seen=previous.seen,
        bootstrapped=previous.bootstrapped,
        fresh=[],
        last_calls=previous.last_calls,
        active_rings=active,
    )
