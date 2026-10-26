"""The Ufanet video intercom integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .api import (
    CameraInfo,
    IntercomInfo,
    UfanetApiAuthError,
    UfanetApiClient,
    UfanetApiError,
)
from .const import (
    AUTH_PASSWORD,
    AUTH_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CAMERA_MAP,
    CONF_CONTRACT,
    CONF_DOM_URL,
    CONF_ENABLED_PANELS,
    CONF_REFRESH_TOKEN,
    CONF_SIP_HOST,
    CONF_SIP_PORT,
    CONF_SIP_USERNAME,
    DEFAULT_DOM_URL,
    DEFAULT_SIP_HOST,
    DEFAULT_SIP_PORT,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_VERSION,
)
from .coordinator import UfanetCallCoordinator
from .helpers import (
    default_camera_map,
    filter_enabled_intercoms,
    intercom_device_id,
    resolve_camera_map,
)
from .sip import SipMonitor
from .visit import async_announce_visit, ensure_blueprint

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SENSOR,
]

type UfanetConfigEntry = ConfigEntry[UfanetRuntimeData]


@dataclass
class UfanetRuntimeData:
    """Runtime objects for a config entry."""

    client: UfanetApiClient
    store: Store
    contract: str
    intercoms: list[IntercomInfo]
    cameras: list[CameraInfo]
    camera_map: dict[str, str]
    coordinator: UfanetCallCoordinator
    hub_device_id: str | None = None
    sip: object | None = None
    last_visit: float = 0.0
    last_visit_key: str = ""


async def _save_tokens(
    store: Store,
    contract: str,
    token: str,
    exp: int | None,
    *,
    access_token: str | None = None,
) -> None:
    """Persist rotated tokens in Home Assistant Store (not entry.data)."""
    stored_data = await store.async_load() or {}
    creds = dict(stored_data.get(contract) or {})
    creds["refresh_token"] = token
    creds["refresh_exp"] = exp
    if access_token:
        creds["access_token"] = access_token
    stored_data[contract] = creds
    await store.async_save(stored_data)


async def _load_credentials(store: Store, contract: str, entry: ConfigEntry) -> dict:
    """Load credentials from Store, falling back to legacy entry.data tokens."""
    stored_data = await store.async_load() or {}
    credentials = dict(stored_data.get(contract) or {})

    # Migrate tokens previously stored in entry.data into Store.
    legacy_refresh = entry.data.get(CONF_REFRESH_TOKEN)
    legacy_access = entry.data.get(CONF_ACCESS_TOKEN)
    changed = False
    if legacy_refresh and not credentials.get("refresh_token"):
        credentials["refresh_token"] = legacy_refresh
        changed = True
    if legacy_access and not credentials.get("access_token"):
        credentials["access_token"] = legacy_access
        changed = True

    refresh_token = credentials.get("refresh_token")
    if refresh_token:
        jwt_exp = UfanetApiClient.extract_exp(refresh_token)
        if jwt_exp and jwt_exp != credentials.get("refresh_exp"):
            credentials["refresh_exp"] = jwt_exp
            changed = True

    if changed:
        stored_data[contract] = credentials
        await store.async_save(stored_data)

    return credentials


def _entry_data_without_secrets(entry: ConfigEntry) -> dict:
    """Return entry data with JWT secrets stripped."""
    data = dict(entry.data)
    data.pop(CONF_ACCESS_TOKEN, None)
    data.pop(CONF_REFRESH_TOKEN, None)
    return data


async def async_setup(_hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration from yaml (not supported)."""
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries."""
    _LOGGER.debug("Migrating %s from version %s", entry.entry_id, entry.version)
    version_auth_method = 2
    version_store_tokens = 3

    if entry.version < version_auth_method:
        data = dict(entry.data)
        data.setdefault(CONF_AUTH_METHOD, AUTH_PASSWORD)
        data.setdefault(CONF_DOM_URL, DEFAULT_DOM_URL)
        hass.config_entries.async_update_entry(
            entry, data=data, version=version_auth_method
        )

    if entry.version < version_store_tokens:
        # Move JWT out of entry.data into Store; keep only non-secret fields.
        contract = entry.data.get(CONF_CONTRACT)
        if contract:
            store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
            await _load_credentials(store, contract, entry)
        hass.config_entries.async_update_entry(
            entry,
            data=_entry_data_without_secrets(entry),
            version=version_store_tokens,
        )

    return True


async def async_setup_entry(  # noqa: PLR0915
    hass: HomeAssistant, entry: UfanetConfigEntry
) -> bool:
    """Set up the Ufanet video intercom from a config entry."""
    contract = entry.data.get(CONF_CONTRACT)
    if not contract:
        msg = "Missing contract number"
        raise ConfigEntryAuthFailed(msg)

    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    credentials = await _load_credentials(store, contract, entry)

    # Strip secrets from entry.data if still present (pre-migration reload).
    if CONF_ACCESS_TOKEN in entry.data or CONF_REFRESH_TOKEN in entry.data:
        hass.config_entries.async_update_entry(
            entry, data=_entry_data_without_secrets(entry)
        )

    auth_method = entry.data.get(CONF_AUTH_METHOD, AUTH_PASSWORD)
    is_phone = auth_method == AUTH_PHONE
    password = None if is_phone else credentials.get("password")

    if not is_phone and not password and not credentials.get("refresh_token"):
        msg = "Missing credentials; please reauthenticate"
        raise ConfigEntryAuthFailed(msg)

    if is_phone and not credentials.get("refresh_token"):
        msg = "Phone session expired; please reauthenticate"
        raise ConfigEntryAuthFailed(msg)

    session = async_get_clientsession(hass)
    client = UfanetApiClient(
        session,
        contract,
        password=password,
        access_token=credentials.get("access_token"),
        refresh_token=credentials.get("refresh_token"),
        refresh_exp=credentials.get("refresh_exp"),
        base_url=entry.data.get(CONF_DOM_URL) or DEFAULT_DOM_URL,
        allow_password_login=not is_phone,
    )

    async def on_token_update(token: str, exp: int | None) -> None:
        await _save_tokens(store, contract, token, exp)

    client.set_token_update_callback(on_token_update)

    try:
        intercoms = await client.async_get_intercoms()
        cameras = await client.async_get_cameras()
    except UfanetApiAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except UfanetApiError as err:
        raise ConfigEntryNotReady(str(err)) from err

    if not intercoms:
        msg = "No intercoms available for this account"
        raise ConfigEntryNotReady(msg)

    intercoms = filter_enabled_intercoms(
        intercoms, entry.options.get(CONF_ENABLED_PANELS)
    )

    # Seed options with API defaults once, so the UI shows current links.
    if CONF_CAMERA_MAP not in entry.options:
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                CONF_CAMERA_MAP: default_camera_map(intercoms),
            },
        )

    camera_map = resolve_camera_map(entry, intercoms)
    coordinator = UfanetCallCoordinator(hass, entry, client)
    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        _LOGGER.warning(
            "Ufanet call history unavailable, doorbell starts when it recovers: %s",
            coordinator.last_exception,
        )

    # Account hub device; intercom devices attach via via_device_id.
    device_registry = dr.async_get(hass)
    hub_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, contract)},
        manufacturer="Ufanet",
        name=f"Ufanet {contract}",
        model="Video intercom account",
    )
    live_identifiers = {intercom_device_id(contract, item.id) for item in intercoms}
    live_identifiers.add(contract)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        own = {ident for domain, ident in device.identifiers if domain == DOMAIN}
        if own and own.isdisjoint(live_identifiers):
            device_registry.async_remove_device(device.id)

    sip = await _start_sip(hass, entry, credentials)
    entry.runtime_data = UfanetRuntimeData(
        client=client,
        store=store,
        contract=contract,
        intercoms=intercoms,
        cameras=cameras,
        camera_map=camera_map,
        coordinator=coordinator,
        hub_device_id=hub_device.id,
        sip=sip,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    await hass.async_add_executor_job(ensure_blueprint, hass)

    def _on_history() -> None:
        if entry.runtime_data.coordinator.snapshot.fresh:
            async_announce_visit(hass, entry, "history")

    entry.async_on_unload(coordinator.async_add_listener(_on_history))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> None:
    """Reload when options (camera map) change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: UfanetConfigEntry) -> bool:
    """Unload a config entry."""
    sip = getattr(entry, "runtime_data", None)
    monitor = getattr(sip, "sip", None) if sip else None
    if isinstance(monitor, SipMonitor):
        await monitor.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _start_sip(
    hass: HomeAssistant, entry: UfanetConfigEntry, credentials: dict
) -> SipMonitor | None:
    """Register the SIP monitor when the app credentials are saved."""
    username = entry.options.get(CONF_SIP_USERNAME) or ""
    password = credentials.get("sip_password") or ""
    if not username or not password:
        return None
    host = str(entry.options.get(CONF_SIP_HOST) or DEFAULT_SIP_HOST)
    port = int(entry.options.get(CONF_SIP_PORT) or DEFAULT_SIP_PORT)

    def on_ring() -> None:
        async_dispatcher_send(hass, f"{DOMAIN}_sip_{entry.entry_id}", "ring")
        async_announce_visit(hass, entry, "sip")

    def on_idle() -> None:
        async_dispatcher_send(hass, f"{DOMAIN}_sip_{entry.entry_id}", "idle")

    monitor = SipMonitor(str(username), str(password), host, port, on_ring, on_idle)
    try:
        await monitor.start()
    except Exception:  # noqa: BLE001
        _LOGGER.warning("SIP monitor did not start", exc_info=True)
        return None
    _LOGGER.info("SIP monitor %s registering at %s:%s", username, host, port)
    return monitor


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry - clean up secure storage."""
    contract = entry.data.get(CONF_CONTRACT)
    if contract:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await store.async_load() or {}
        stored_data.pop(contract, None)
        await store.async_save(stored_data)
