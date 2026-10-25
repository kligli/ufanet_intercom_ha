"""Ringing sensor for a Ufanet video intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
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

# Doorbell exists only on newer Home Assistant. Older cores raise AttributeError
# while the platform module is imported, which aborts the whole config entry.
_DOORBELL_CLASS = getattr(BinarySensorDeviceClass, "DOORBELL", None)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a ringing sensor for every intercom."""
    async_add_entities(
        [
            UfanetRingingBinarySensor(entry, intercom)
            for intercom in entry.runtime_data.intercoms
        ]
    )


class UfanetRingingBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """On while a recent call is still inside the ring window."""

    _attr_device_class = _DOORBELL_CLASS
    _attr_has_entity_name = True
    _attr_translation_key = "ringing"
    _attr_icon = "mdi:doorbell"

    def __init__(self, entry: UfanetConfigEntry, intercom: IntercomInfo) -> None:
        """Initialize the ringing sensor."""
        super().__init__(entry.runtime_data.coordinator)
        self._entry_id = entry.entry_id
        self._sip_on = False
        contract = entry.runtime_data.contract
        self._camera_number = entry.runtime_data.camera_map.get(str(intercom.id))
        self._label = intercom_display_name(intercom)
        self._attr_unique_id = f"{contract}_{intercom.id}_ringing"
        self._attr_suggested_object_id = f"{contract}_{intercom.id}_ringing"
        self._attr_device_info = intercom_device_info(
            contract,
            intercom,
            hub_device_id=entry.runtime_data.hub_device_id,
        )

    async def async_added_to_hass(self) -> None:
        """Follow the SIP monitor as well as call history."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{DOMAIN}_sip_{self._entry_id}", self._on_sip
            )
        )

    @callback
    def _on_sip(self, kind: str) -> None:
        """Turn the sensor on for a SIP ring and off when the call ends."""
        self._sip_on = kind != "idle"
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Stay on the device while call history is catching up."""
        return True

    @property
    def is_on(self) -> bool:
        """Return True while the intercom call is still current."""
        if self._sip_on:
            return True
        active = self.coordinator.snapshot.active_rings
        if self._camera_number:
            return self._camera_number in active
        return any(
            call.camera_number in active and call_matches_panel(call, None, self._label)
            for call in self.coordinator.snapshot.last_calls.values()
        )
