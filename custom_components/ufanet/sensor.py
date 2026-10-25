"""Last-call sensor for a Ufanet video intercom."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import intercom_display_name
from .calls import CallRecord, call_matches_panel
from .helpers import intercom_device_info

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from . import UfanetConfigEntry
    from .api import IntercomInfo


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: UfanetConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a last-call sensor for every intercom."""
    async_add_entities(
        [
            UfanetLastCallSensor(entry, intercom)
            for intercom in entry.runtime_data.intercoms
        ]
    )


class UfanetLastCallSensor(CoordinatorEntity, SensorEntity):
    """Timestamp of the latest call to this intercom."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_has_entity_name = True
    _attr_translation_key = "last_call"
    _attr_icon = "mdi:history"

    def __init__(self, entry: UfanetConfigEntry, intercom: IntercomInfo) -> None:
        """Initialize the last-call sensor."""
        super().__init__(entry.runtime_data.coordinator)
        contract = entry.runtime_data.contract
        self._camera_number = entry.runtime_data.camera_map.get(str(intercom.id))
        self._label = intercom_display_name(intercom)
        self._attr_unique_id = f"{contract}_{intercom.id}_last_call"
        self._attr_suggested_object_id = f"{contract}_{intercom.id}_last_call"
        self._attr_device_info = intercom_device_info(
            contract,
            intercom,
            hub_device_id=entry.runtime_data.hub_device_id,
        )

    @property
    def available(self) -> bool:
        """Stay on the device while call history is catching up."""
        return True

    @property
    def native_value(self) -> datetime | None:
        """Return the time of the latest call."""
        call = self._latest_call()
        return call.called_at if call else None

    def _latest_call(self) -> CallRecord | None:
        """Return the newest history row for this panel."""
        matched = [
            call
            for call in self.coordinator.snapshot.last_calls.values()
            if call_matches_panel(call, self._camera_number, self._label)
        ]
        if not matched:
            return None
        return max(matched, key=lambda call: call.called_at)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose address details from the latest call."""
        call = self._latest_call()
        if call is None:
            return None
        return {
            "uuid": call.uuid,
            "address": call.address,
            "porch": call.porch,
            "flat": call.flat,
            "camera_number": call.camera_number,
        }
