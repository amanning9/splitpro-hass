"""Split Pro: add expenses from Home Assistant and track balances, using Split Pro's REST API."""

from __future__ import annotations

from homeassistant.const import CONF_API_KEY, CONF_URL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import SplitProClient
from .const import CONF_VERIFY_SSL, DOMAIN
from .coordinator import SplitProConfigEntry, SplitProCoordinator, account_device_info
from .services import async_register_services

PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the actions (they work for every Split Pro entry)."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SplitProConfigEntry) -> bool:
    verify_ssl = entry.data.get(CONF_VERIFY_SSL, True)
    client = SplitProClient(
        async_get_clientsession(hass, verify_ssl=verify_ssl), entry.data[CONF_URL], entry.data[CONF_API_KEY], verify_ssl
    )
    coordinator = SplitProCoordinator(hass, entry, client)
    # The account device first, so group and friend devices can hang off it.
    dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, **account_device_info(entry))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SplitProConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
