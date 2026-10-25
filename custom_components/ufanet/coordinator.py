"""Poll Ufanet call history and publish doorbell rings."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import UfanetApiError
from .calls import (
    CALL_HISTORY_INTERVAL,
    RING_WINDOW,
    CallRecord,
    HistorySnapshot,
    apply_history_update,
    expire_rings,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .api import UfanetApiClient

_LOGGER = logging.getLogger(__name__)


class UfanetCallCoordinator(DataUpdateCoordinator[list[CallRecord]]):
    """Fetch recent intercom calls and keep doorbell ring state."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: UfanetApiClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Ufanet doorbell",
            update_interval=CALL_HISTORY_INTERVAL,
            config_entry=entry,
        )
        self.client = client
        self.snapshot = HistorySnapshot()
        self._ring_unsub: Callable[[], None] | None = None

    async def _async_update_data(self) -> list[CallRecord]:
        """Poll call history and refresh ring state."""
        try:
            calls = await self.client.async_get_call_history()
        except UfanetApiError as err:
            raise UpdateFailed(str(err)) from err

        now = dt_util.utcnow()
        self.snapshot = apply_history_update(self.snapshot, calls, now)
        for call in self.snapshot.fresh:
            _LOGGER.info(
                "Doorbell ring camera=%s at=%s address=%s",
                call.camera_number,
                call.called_at.isoformat(),
                call.address,
            )
        self._arm_ring_timer(now)
        return calls

    @callback
    def _arm_ring_timer(self, now: datetime) -> None:
        """Turn the ringing sensor off when the ring window ends."""
        if self._ring_unsub is not None:
            self._ring_unsub()
            self._ring_unsub = None
        if not self.snapshot.active_rings:
            return
        expiry = min(self.snapshot.active_rings.values()) + RING_WINDOW
        delay = (expiry - now).total_seconds()
        if delay <= 0:
            return
        self._ring_unsub = async_call_later(self.hass, delay, self._on_ring_window_end)

    @callback
    def _on_ring_window_end(self, now: datetime) -> None:
        """Clear expired rings and update doorbell entities."""
        self._ring_unsub = None
        self.snapshot = expire_rings(self.snapshot, now)
        self.async_update_listeners()
        self._arm_ring_timer(now)

    async def async_shutdown(self) -> None:
        """Stop polling and cancel the ring timer."""
        if self._ring_unsub is not None:
            self._ring_unsub()
            self._ring_unsub = None
        await super().async_shutdown()
