"""Polls Split Pro for groups, friends and balances, and keeps a device per group and friend."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import SplitProAuthError, SplitProClient, SplitProError
from .const import CONF_GROUPS, DOMAIN, UPDATE_INTERVAL
from .money import DEFAULT_CURRENCY, is_currency

_LOGGER = logging.getLogger(__name__)

type SplitProConfigEntry = ConfigEntry[SplitProCoordinator]


@dataclass
class Member:
    """A Split Pro user, as far as the integration cares."""

    id: int
    name: str
    email: str | None

    @classmethod
    def from_api(cls, user: dict[str, Any]) -> Member:
        return cls(user["id"], user.get("name") or user.get("email") or f"User {user['id']}", user.get("email"))


@dataclass
class Group:
    """An active (not archived) group you're in."""

    id: int
    name: str
    default_currency: str | None
    default_split: dict[str, Any] | None
    members: list[Member]
    # Only for groups with a balance sensor: your net balance per currency, and each member's.
    balances: dict[str, int] | None = None
    member_balances: dict[int, dict[str, int]] | None = None


@dataclass
class SplitProData:
    """Everything one update fetches."""

    me: Member
    my_default_currency: str | None
    groups: dict[int, Group]
    friends: dict[int, Member]
    # Your net balance with each friend per currency, across groups and non-group expenses.
    friend_balances: dict[int, dict[str, int]] = field(default_factory=dict)

    @property
    def total(self) -> dict[str, int]:
        """Your net balance per currency across everyone. Positive = you're owed."""
        total: dict[str, int] = {}
        for balances in self.friend_balances.values():
            for currency, amount in balances.items():
                total[currency] = total.get(currency, 0) + amount
        return {currency: amount for currency, amount in total.items() if amount}

    def fallback_currency(self, group: Group | None = None) -> str:
        """Currency for a zero balance or a new expense: group's default, then yours, then GBP."""
        for currency in (group.default_currency if group else None, self.my_default_currency):
            if is_currency(currency):
                return currency  # type: ignore[return-value]
        return DEFAULT_CURRENCY


class SplitProCoordinator(DataUpdateCoordinator[SplitProData]):
    """Fetches Split Pro data every few minutes, and straight after the integration writes."""

    config_entry: SplitProConfigEntry

    def __init__(self, hass: HomeAssistant, entry: SplitProConfigEntry, client: SplitProClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = client
        self.async_add_listener(self._sync_devices)

    async def _async_update_data(self) -> SplitProData:
        try:
            return await self._fetch()
        except SplitProAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SplitProError as err:
            raise UpdateFailed(str(err)) from err

    async def _fetch(self) -> SplitProData:
        client = self.client
        me = await client.me()
        groups = {
            group["id"]: Group(
                id=group["id"],
                name=group["name"],
                default_currency=group.get("defaultCurrency"),
                default_split=group.get("defaultSplit"),
                members=[Member.from_api(gu["user"]) for gu in group.get("groupUsers", [])],
            )
            for group in await client.groups()
            if not group.get("archivedAt")
        }
        friends = {friend["id"]: Member.from_api(friend) for friend in await client.friends()}
        friend_balances = {
            entry["friendId"]: {c["currency"]: int(c["amount"]) for c in entry["currencies"] if c["amount"]}
            for entry in await client.balances()
        }
        data = SplitProData(Member.from_api(me), me.get("defaultCurrency"), groups, friends, friend_balances)

        # Group balances need one call per group, so only for groups with a sensor.
        for group_id in self.config_entry.options.get(CONF_GROUPS, []):
            group = groups.get(int(group_id))
            if group is None:
                continue
            details = await client.group_details(group.id)
            group.balances, group.member_balances = _group_balances(details.get("groupBalances", []), data.me.id)
        return data

    @callback
    def _sync_devices(self) -> None:
        """One device per group and friend (so actions can target them), removing ones that went away."""
        if self.data is None:
            return
        registry = dr.async_get(self.hass)
        entry = self.config_entry
        wanted = {account_identifier(entry)}
        for group in self.data.groups.values():
            wanted.add(group_identifier(entry, group.id))
            registry.async_get_or_create(config_entry_id=entry.entry_id, **group_device_info(entry, group))
        for friend in self.data.friends.values():
            wanted.add(friend_identifier(entry, friend.id))
            registry.async_get_or_create(config_entry_id=entry.entry_id, **friend_device_info(entry, friend))
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
            if not device.identifiers & wanted:
                registry.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


def _group_balances(rows: list[dict[str, Any]], my_id: int) -> tuple[dict[str, int], dict[int, dict[str, int]]]:
    """Your net balance per currency in a group, and each member's (from BalanceView rows).

    A row {userId, friendId, currency, amount} means friendId owes userId `amount` (negative: the
    other way); every pair appears in both directions, so each member's net is the sum of their rows.
    """
    per_member: dict[int, dict[str, int]] = {}
    for row in rows:
        member = per_member.setdefault(row["userId"], {})
        member[row["currency"]] = member.get(row["currency"], 0) + int(row["amount"])
    per_member = {uid: {c: a for c, a in b.items() if a} for uid, b in per_member.items()}
    return per_member.get(my_id, {}), per_member


def account_identifier(entry: ConfigEntry) -> tuple[str, str]:
    return (DOMAIN, entry.entry_id)


def group_identifier(entry: ConfigEntry, group_id: int) -> tuple[str, str]:
    return (DOMAIN, f"{entry.entry_id}_group_{group_id}")


def friend_identifier(entry: ConfigEntry, friend_id: int) -> tuple[str, str]:
    return (DOMAIN, f"{entry.entry_id}_friend_{friend_id}")


def account_device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={account_identifier(entry)},
        name=entry.title,
        manufacturer="Split Pro",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=entry.data["url"],
    )


def group_device_info(entry: ConfigEntry, group: Group) -> DeviceInfo:
    return DeviceInfo(
        identifiers={group_identifier(entry, group.id)},
        name=group.name,
        manufacturer="Split Pro",
        model="Group",
        entry_type=DeviceEntryType.SERVICE,
        via_device=account_identifier(entry),
    )


def friend_device_info(entry: ConfigEntry, friend: Member) -> DeviceInfo:
    return DeviceInfo(
        identifiers={friend_identifier(entry, friend.id)},
        name=friend.name,
        manufacturer="Split Pro",
        model="Friend",
        entry_type=DeviceEntryType.SERVICE,
        via_device=account_identifier(entry),
    )
