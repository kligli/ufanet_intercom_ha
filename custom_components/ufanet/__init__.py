"""The Ufanet Intercom integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable  # noqa: TC003
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.storage import Store

from .const import (
    AUTH_PHONE,
    CONF_ACCESS_TOKEN,
    CONF_AUTH_METHOD,
    CONF_CONTRACT,
    CONF_DOM_URL,
    CONF_REFRESH_TOKEN,
    DEFAULT_DOM_URL,
    DOMAIN,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

STORAGE_KEY = f"{DOMAIN}_credentials"
STORAGE_VERSION = 1

PLATFORMS: list[Platform] = [Platform.BUTTON, Platform.CAMERA]


def create_token_save_callback(
    store: Store | None,
    contract: str | None,
    *,
    hass: HomeAssistant | None = None,
    entry: ConfigEntry | None = None,
) -> Callable[[str, int | None], Awaitable[None]] | None:
    """Create a callback to persist rotated refresh tokens."""
    if not store or not contract:
        return None

    async def save_token(token: str, exp: int | None) -> None:
        stored_data = await store.async_load() or {}
        if contract not in stored_data:
            stored_data[contract] = {}
        stored_data[contract]["refresh_token"] = token
        stored_data[contract]["refresh_exp"] = exp
        await store.async_save(stored_data)

        # Phone accounts can't re-login — keep entry.data in sync after refresh.
        if (
            hass
            and entry
            and entry.data.get(CONF_AUTH_METHOD) == AUTH_PHONE
        ):
            hass.config_entries.async_update_entry(
                entry,
                data={
                    **entry.data,
                    CONF_REFRESH_TOKEN: token,
                },
            )

    return save_token


async def get_password_from_storage(
    store: Store | None, contract: str | None
) -> str | None:
    """Get password from secure storage."""
    if not store or not contract:
        return None
    stored_data = await store.async_load() or {}
    credentials = stored_data.get(contract, {})
    return credentials.get("password")


def build_api_client_kwargs(entry: ConfigEntry, data: dict) -> dict:
    """Shared kwargs for UfanetApiClient from entry + platform data."""
    auth_method = entry.data.get(CONF_AUTH_METHOD, data.get(CONF_AUTH_METHOD))
    is_phone = auth_method == AUTH_PHONE
    return {
        "access_token": data.get("access_token")
        or entry.data.get(CONF_ACCESS_TOKEN),
        "refresh_token": data.get("refresh_token")
        or entry.data.get(CONF_REFRESH_TOKEN),
        "refresh_exp": data.get("refresh_exp"),
        "base_url": entry.data.get(CONF_DOM_URL)
        or data.get(CONF_DOM_URL)
        or DEFAULT_DOM_URL,
        "allow_password_login": not is_phone,
    }


async def async_setup(_hass: HomeAssistant, _config: dict) -> bool:
    """Set up the integration from yaml (not supported)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Ufanet Intercom from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    contract = entry.data.get(CONF_CONTRACT)
    store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stored_data = await store.async_load() or {}
    credentials = stored_data.get(contract, {}) if contract else {}

    refresh_token = credentials.get("refresh_token") or entry.data.get(
        CONF_REFRESH_TOKEN
    )
    refresh_exp = credentials.get("refresh_exp")
    access_token = credentials.get("access_token") or entry.data.get(
        CONF_ACCESS_TOKEN
    )

    if refresh_token:
        from .api import UfanetApiClient

        jwt_exp = UfanetApiClient.extract_exp(refresh_token)
        if jwt_exp and jwt_exp != refresh_exp:
            refresh_exp = jwt_exp
            if contract:
                credentials = dict(credentials)
                credentials["refresh_token"] = refresh_token
                credentials["refresh_exp"] = jwt_exp
                if access_token:
                    credentials["access_token"] = access_token
                stored_data[contract] = credentials
                await store.async_save(stored_data)

    platform_data = dict(entry.data)
    platform_data["refresh_token"] = refresh_token
    platform_data["refresh_exp"] = refresh_exp
    platform_data["access_token"] = access_token
    platform_data["_store"] = store
    platform_data["_contract"] = contract
    platform_data["_entry"] = entry

    hass.data[DOMAIN][entry.entry_id] = platform_data
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle removal of an entry - clean up secure storage."""
    contract = entry.data.get(CONF_CONTRACT)
    if contract:
        store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        stored_data = await store.async_load() or {}
        stored_data.pop(contract, None)
        await store.async_save(stored_data)
