"""Balance sensors: total, per group and per friend, each only if chosen in the options.

A sensor shows its balance in that currency when everything it covers is in one currency, and is
unavailable while it covers more than one (an HA sensor has a single unit). Positive means you're
owed; a zero balance shows 0 in the group's default currency, else yours, else GBP.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FRIENDS, CONF_GROUPS, CONF_TOTAL, DOMAIN
from .coordinator import (
    Group,
    SplitProConfigEntry,
    SplitProCoordinator,
    account_device_info,
    friend_device_info,
    group_device_info,
)
from .money import from_minor_units


async def async_setup_entry(
    hass: HomeAssistant, entry: SplitProConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    data = coordinator.data
    entities: list[BalanceSensor] = []
    if entry.options.get(CONF_TOTAL):
        entities.append(TotalBalanceSensor(coordinator))
    for group_id in entry.options.get(CONF_GROUPS, []):
        if (group := data.groups.get(int(group_id))) is not None:
            entities.append(GroupBalanceSensor(coordinator, group))
    for friend_id in entry.options.get(CONF_FRIENDS, []):
        if int(friend_id) in data.friends:
            entities.append(FriendBalanceSensor(coordinator, int(friend_id)))

    # Remove sensors that were deselected in the options (or whose group/friend went away).
    registry = er.async_get(hass)
    keep = {entity.unique_id for entity in entities}
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.domain == "sensor" and reg_entry.unique_id not in keep:
            registry.async_remove(reg_entry.entity_id)

    async_add_entities(entities)


class BalanceSensor(CoordinatorEntity[SplitProCoordinator], SensorEntity):
    """A net balance in one currency."""

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def _balances(self) -> dict[str, int] | None:
        """The balance per currency (non-zero only), or None if unknown."""
        raise NotImplementedError

    def _zero_currency(self) -> str:
        return self.coordinator.data.fallback_currency()

    @property
    def available(self) -> bool:
        balances = self._balances()
        return super().available and balances is not None and len(balances) <= 1

    @property
    def native_unit_of_measurement(self) -> str:
        balances = self._balances() or {}
        return next(iter(balances), None) or self._zero_currency()

    @property
    def native_value(self) -> float | None:
        balances = self._balances()
        if balances is None or len(balances) > 1:
            return None
        currency = self.native_unit_of_measurement
        return from_minor_units(balances.get(currency, 0), currency)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"balances": _format(self._balances() or {})}


class TotalBalanceSensor(BalanceSensor):
    """Your net balance across all groups and friends."""

    _attr_translation_key = "total_balance"

    def __init__(self, coordinator: SplitProCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_total_balance"
        self._attr_device_info = account_device_info(entry)

    def _balances(self) -> dict[str, int] | None:
        return self.coordinator.data.total


class GroupBalanceSensor(BalanceSensor):
    """Your net balance in one group."""

    _attr_translation_key = "group_balance"

    def __init__(self, coordinator: SplitProCoordinator, group: Group) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.group_id = group.id
        self._attr_unique_id = f"{entry.entry_id}_group_{group.id}_balance"
        self._attr_device_info = group_device_info(entry, group)

    @property
    def _group(self) -> Group | None:
        return self.coordinator.data.groups.get(self.group_id)

    def _balances(self) -> dict[str, int] | None:
        group = self._group
        return None if group is None else group.balances

    def _zero_currency(self) -> str:
        return self.coordinator.data.fallback_currency(self._group)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        group = self._group
        if group is None:
            return {}
        member_balances = group.member_balances or {}
        return {
            "group_id": group.id,
            "balances": _format(group.balances or {}),
            "members": {
                member.name: _format(member_balances.get(member.id, {})) for member in group.members
            },
        }


class FriendBalanceSensor(BalanceSensor):
    """Your net balance with one friend, across groups and non-group expenses."""

    _attr_translation_key = "friend_balance"

    def __init__(self, coordinator: SplitProCoordinator, friend_id: int) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.friend_id = friend_id
        self._attr_unique_id = f"{entry.entry_id}_friend_{friend_id}_balance"
        self._attr_device_info = friend_device_info(entry, coordinator.data.friends[friend_id])

    def _balances(self) -> dict[str, int] | None:
        data = self.coordinator.data
        if self.friend_id not in data.friends:
            return None
        return data.friend_balances.get(self.friend_id, {})

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"friend_id": self.friend_id, "balances": _format(self._balances() or {})}


def _format(balances: dict[str, int]) -> dict[str, float]:
    """{currency: amount in main units} for attributes."""
    return {currency: from_minor_units(amount, currency) for currency, amount in sorted(balances.items())}
