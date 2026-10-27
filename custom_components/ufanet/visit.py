"""One path for a door visit: ring, answer the SIP monitor, show the camera."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING

from homeassistant.components.persistent_notification import async_create
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import intercom_display_name
from .calls import visit_notice, visit_panel_ids
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import EntityRegistry

    from . import UfanetConfigEntry

_LOGGER = logging.getLogger(__name__)

# Written once. Home Assistant reads it on the next start.
_BLUEPRINT = """blueprint:
  name: Ufanet doorbell
  description: >
    Notify when the intercom rings, attach the camera picture,
    and open the door from the phone notification.
  domain: automation
  input:
    doorbell:
      name: Doorbell
      selector:
        entity:
          domain: event
    camera:
      name: Camera
      selector:
        entity:
          domain: camera
    door:
      name: Open door
      selector:
        entity:
          domain: button
    notify_service:
      name: Notify service
      description: Mobile app service, for example mobile_app_phone.
      selector:
        text:
mode: single
max_exceeded: silent
triggers:
  - trigger: state
    entity_id: !input doorbell
variables:
  camera_entity: !input camera
  door_entity: !input door
  notify_service: !input notify_service
actions:
  - action: "notify.{{ notify_service }}"
    data:
      title: Doorbell
      message: Someone is at the door
      data:
        image: "{{ '/api/camera_proxy/' ~ camera_entity }}"
        actions:
          - action: OPEN_DOOR
            title: Open
  - wait_for_trigger:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: OPEN_DOOR
    timeout: "00:02:00"
    continue_on_timeout: false
  - action: button.press
    target:
      entity_id: "{{ door_entity }}"
"""


def ensure_blueprint(hass: HomeAssistant) -> None:
    """Install the doorbell blueprint once, without overwriting edits."""
    path = Path(hass.config.path("blueprints", "automation", "ufanet", "visit.yaml"))
    allowed = getattr(hass.config, "is_allowed_path", None)
    if allowed is not None and not allowed(str(path)):
        return
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_BLUEPRINT, encoding="utf-8")


def _entity_id(registry: EntityRegistry, entry_id: str, unique_id: str) -> str | None:
    """Return the entity id for a unique id on this config entry."""
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.unique_id == unique_id:
            return entity.entity_id
    return None


def async_announce_visit(
    hass: HomeAssistant, entry: UfanetConfigEntry, source: str
) -> None:
    """Ring, answer the monitor, and leave a notification with the camera."""
    runtime = entry.runtime_data
    sip = runtime.sip
    sip_answered = False
    if source == "sip" and sip is not None and sip.ringing and not sip.in_call:
        sip.answer()
        sip_answered = True

    fresh = list(runtime.coordinator.snapshot.fresh)
    if source == "sip" and sip is not None and getattr(sip, "call", None) is not None:
        key = f"sip:{sip.call.call_id}"
    elif fresh:
        key = "history:" + ",".join(sorted(call.uuid for call in fresh))
    else:
        key = f"{source}:{time.monotonic():.0f}"
    if key == runtime.last_visit_key:
        return
    runtime.last_visit_key = key
    runtime.last_visit = time.monotonic()

    panels = [
        (
            intercom.id,
            runtime.camera_map.get(str(intercom.id)),
            intercom_display_name(intercom),
        )
        for intercom in runtime.intercoms
    ]
    # SIP does not name the door. A leftover history row must not pick it.
    # The history poll that just arrived is the one that narrows the panel.
    matched_calls = fresh if source == "history" else []
    chosen = set(visit_panel_ids(panels, matched_calls))
    registry = er.async_get(hass)
    camera_id = None
    door_id = None
    address = None
    for intercom_id, _camera, label in panels:
        if intercom_id not in chosen:
            continue
        camera_entity = _entity_id(
            registry, entry.entry_id, f"{runtime.contract}_{intercom_id}_camera"
        )
        door_entity = _entity_id(
            registry, entry.entry_id, f"{runtime.contract}_{intercom_id}"
        )
        camera_id = camera_id or camera_entity
        door_id = door_id or door_entity
        address = address or label
    if not sip_answered:
        _LOGGER.info(
            "Doorbell at %s came from %s; SIP monitor did not take the call",
            address,
            source,
        )
    async_create(
        hass,
        visit_notice(address, camera_id, door_id, sip_answered=sip_answered),
        title="Домофон",
        notification_id=f"{DOMAIN}_visit_{entry.entry_id}",
    )
    hass.bus.async_fire(
        f"{DOMAIN}_visit",
        {
            "entry_id": entry.entry_id,
            "source": source,
            "camera": camera_id,
            "door": door_id,
        },
    )
    if source == "sip":
        async_dispatcher_send(hass, f"{DOMAIN}_sip_{entry.entry_id}", "active")
