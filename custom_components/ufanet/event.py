"""Doorbell event for a Ufanet video intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import intercom_display_name
from .calls import call_matches_panel
from .const import DOMAIN
from .helpers import intercom_device_info

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import UfanetConfigEntry
    from .api import IntercomInfo

RING = "ring"


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a doorbell event for every intercom."""
    async_add_entities(
        [
            UfanetDoorbellEvent(entry, intercom)
            for intercom in entry.runtime_data.intercoms
        ]
    )


class UfanetDoorbellEvent(CoordinatorEntity, EventEntity):
    """Fires ``ring`` when somebody calls this intercom."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_has_entity_name = True
    _attr_translation_key = "doorbell"
    _attr_icon = "mdi:doorbell-video"

    def __init__(self, entry: UfanetConfigEntry, intercom: IntercomInfo) -> None:
        """Initialize the doorbell event."""
        super().__init__(entry.runtime_data.coordinator)
        self._attr_event_types = [RING]
        self._entry_id = entry.entry_id
        contract = entry.runtime_data.contract
        self._camera_number = entry.runtime_data.camera_map.get(str(intercom.id))
        self._label = intercom_display_name(intercom)
        # A SIP INVITE does not name the door. One panel can ring at once.
        # Several panels wait for call history, which knows the camera.
        self._sip_rings_here = len(entry.runtime_data.intercoms) == 1
        self._attr_unique_id = f"{contract}_{intercom.id}_doorbell"
        self._attr_suggested_object_id = f"{contract}_{intercom.id}_doorbell"
        self._attr_device_info = intercom_device_info(
            contract,
            intercom,
            hub_device_id=entry.runtime_data.hub_device_id,
        )

    async def async_added_to_hass(self) -> None:
        """Ring immediately when the SIP monitor is called."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_sip_{self._entry_id}", self._on_sip_ring
            )
        )

    @callback
    def _on_sip_ring(self, kind: str) -> None:
        """Publish a SIP ring on this doorbell."""
        if kind != "ring" or not self._sip_rings_here:
            return
        self._trigger_event(RING, {"source": "sip"})
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Stay on the device while call history is catching up."""
        return True

    @callback
    def _handle_coordinator_update(self) -> None:
        """Announce new rings. Startup history is not replayed."""
        for call in self.coordinator.snapshot.fresh:
            if not call_matches_panel(call, self._camera_number, self._label):
                continue
            self._trigger_event(
                RING,
                {
                    "uuid": call.uuid,
                    "called_at": call.called_at.isoformat(),
                    "address": call.address,
                    "porch": call.porch,
                    "flat": call.flat,
                    "camera_number": call.camera_number,
                },
            )
            self.async_write_ha_state()
        super()._handle_coordinator_update()
