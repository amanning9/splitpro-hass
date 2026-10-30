"""Config flow: connect with a URL and API key, then choose which balance sensors to create."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_API_KEY, CONF_URL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SplitProAuthError, SplitProClient, SplitProError
from .const import CONF_FRIENDS, CONF_GROUPS, CONF_TOTAL, CONF_VERIFY_SSL, DOMAIN
from .coordinator import Member, SplitProConfigEntry

_LOGGER = logging.getLogger(__name__)


def _connection_schema(defaults: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_URL, default=defaults.get(CONF_URL, "")): TextSelector(
                TextSelectorConfig(type=TextSelectorType.URL)
            ),
            vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            vol.Required(CONF_VERIFY_SSL, default=defaults.get(CONF_VERIFY_SSL, True)): bool,
        }
    )


def _sensors_schema(
    groups: list[SelectOptionDict], friends: list[SelectOptionDict], current: Mapping[str, Any]
) -> vol.Schema:
    """Which balance sensors to create; nothing is chosen by default."""

    def choose(options: list[SelectOptionDict]) -> SelectSelector:
        return SelectSelector(SelectSelectorConfig(options=options, multiple=True, sort=True))

    valid_groups = {option["value"] for option in groups}
    valid_friends = {option["value"] for option in friends}
    return vol.Schema(
        {
            vol.Required(CONF_TOTAL, default=current.get(CONF_TOTAL, False)): bool,
            # Drop choices that no longer exist, so the form doesn't reject its own defaults.
            vol.Optional(
                CONF_GROUPS, default=[g for g in current.get(CONF_GROUPS, []) if g in valid_groups]
            ): choose(groups),
            vol.Optional(
                CONF_FRIENDS, default=[f for f in current.get(CONF_FRIENDS, []) if f in valid_friends]
            ): choose(friends),
        }
    )


async def _choices(client: SplitProClient) -> tuple[list[SelectOptionDict], list[SelectOptionDict]]:
    """Active groups and friends, as select options keyed by id."""
    groups = [
        SelectOptionDict(value=str(group["id"]), label=group["name"])
        for group in await client.groups()
        if not group.get("archivedAt")
    ]
    friends = [
        SelectOptionDict(value=str(member.id), label=member.name)
        for member in map(Member.from_api, await client.friends())
    ]
    return groups, friends


class SplitProConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add a Split Pro account."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._title = ""
        self._client: SplitProClient | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: SplitProConfigEntry) -> SplitProOptionsFlow:
        return SplitProOptionsFlow()

    async def _connect(self, user_input: dict[str, Any], errors: dict[str, str]) -> dict[str, Any] | None:
        """Check the URL and key with user.me; returns the user, or None with `errors` filled in."""
        client = SplitProClient(
            async_get_clientsession(self.hass, verify_ssl=user_input[CONF_VERIFY_SSL]),
            user_input[CONF_URL],
            user_input[CONF_API_KEY],
            user_input[CONF_VERIFY_SSL],
        )
        try:
            me = await client.me()
        except SplitProAuthError:
            errors["base"] = "invalid_auth"
        except SplitProError as err:
            _LOGGER.debug("Couldn't connect to Split Pro: %s", err)
            errors["base"] = "cannot_connect"
        else:
            self._client = client
            return me
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].strip().rstrip("/")
            if not urlparse(user_input[CONF_URL]).scheme.startswith("http"):
                errors[CONF_URL] = "invalid_url"
            elif (me := await self._connect(user_input, errors)) is not None:
                await self.async_set_unique_id(_unique_id(user_input[CONF_URL], me["id"]))
                self._abort_if_unique_id_configured()
                self._data = user_input
                self._title = f"{me.get('name') or me.get('email')} ({urlparse(user_input[CONF_URL]).netloc})"
                return await self.async_step_sensors()
        return self.async_show_form(
            step_id="user", data_schema=_connection_schema(user_input or {}), errors=errors
        )

    async def async_step_sensors(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title=self._title, data=self._data, options=user_input)
        assert self._client is not None
        try:
            groups, friends = await _choices(self._client)
        except SplitProError:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="sensors", data_schema=_sensors_schema(groups, friends, {}))

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**entry.data, CONF_API_KEY: user_input[CONF_API_KEY]}
            if (me := await self._connect(data, errors)) is not None:
                await self.async_set_unique_id(_unique_id(data[CONF_URL], me["id"]))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(entry, data=data)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {vol.Required(CONF_API_KEY): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))}
            ),
            description_placeholders={"url": entry.data[CONF_URL]},
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            user_input[CONF_URL] = user_input[CONF_URL].strip().rstrip("/")
            if (me := await self._connect(user_input, errors)) is not None:
                # The URL may change (that's what reconfiguring is for), but not the account.
                if entry.unique_id is not None and entry.unique_id.rsplit(":", 1)[-1] != str(me["id"]):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, unique_id=_unique_id(user_input[CONF_URL], me["id"]), data=user_input
                )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_connection_schema(user_input or entry.data), errors=errors
        )


class SplitProOptionsFlow(OptionsFlowWithReload):
    """Change which balance sensors exist."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        entry: SplitProConfigEntry = self.config_entry
        try:
            groups, friends = await _choices(entry.runtime_data.client)
        except SplitProError:
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="init", data_schema=_sensors_schema(groups, friends, entry.options))


def _unique_id(url: str, user_id: int) -> str:
    """One entry per Split Pro user per server."""
    return f"{urlparse(url).netloc.lower()}:{user_id}"
