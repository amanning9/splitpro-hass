"""Actions: add an expense to a group or with a friend, settle up, and refresh balances."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from .api import SplitProError, SplitProRequestError
from .const import DOMAIN, SERVICE_ADD_EXPENSE, SERVICE_REFRESH, SERVICE_SETTLE_UP
from .coordinator import Group, Member, SplitProConfigEntry, SplitProCoordinator
from .money import CATEGORIES, CATEGORY_OPTIONS, DEFAULT_CATEGORY, from_minor_units, is_currency, to_minor_units
from .split import Split, SplitError, SplitType, equal, participant_amounts

_LOGGER = logging.getLogger(__name__)

SPLIT_DEFAULT = "default"
SPLIT_EQUAL = "equal"
SPLIT_PERCENTAGE = "percentage"
SPLIT_SHARES = "shares"
SPLIT_EXACT = "exact"



def _decimal(value: Any) -> Decimal:
    """A number as typed: via str(), so a float like 4.66 doesn't become 4.66000000000000014..."""
    try:
        return Decimal(str(value).strip())
    except ArithmeticError as err:
        raise vol.Invalid(f"not a number: {value}") from err


_SHARES = vol.Schema({cv.string: vol.All(_decimal, vol.Range(min=0))})

ADD_EXPENSE_SCHEMA = vol.Schema(
    {
        # A Split Pro group or friend device.
        vol.Required("device"): cv.string,
        vol.Required("description"): vol.All(cv.string, vol.Length(min=1)),
        vol.Required("amount"): vol.All(_decimal, vol.Range(min=0, min_included=False)),
        vol.Optional("currency"): vol.All(cv.string, vol.Upper),
        vol.Optional("paid_by"): cv.string,
        vol.Optional("split", default=SPLIT_DEFAULT): vol.In(
            [SPLIT_DEFAULT, SPLIT_EQUAL, SPLIT_PERCENTAGE, SPLIT_SHARES, SPLIT_EXACT]
        ),
        vol.Optional("participants"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("shares"): _SHARES,
        vol.Optional("category", default=DEFAULT_CATEGORY): vol.All(
            cv.string, vol.In([*CATEGORY_OPTIONS, *CATEGORIES]), lambda c: CATEGORY_OPTIONS.get(c, c)
        ),
        # Date first, so "2026-10-30" means that day (midday) rather than midnight.
        vol.Optional("date"): vol.Any(cv.date, cv.datetime),
    }
)

SETTLE_UP_SCHEMA = vol.Schema(
    {
        # A Split Pro group or friend device.
        vol.Required("device"): cv.string,
        vol.Required("amount"): vol.All(_decimal, vol.Range(min=0, min_included=False)),
        vol.Optional("currency"): vol.All(cv.string, vol.Upper),
        vol.Optional("payer"): cv.string,
        vol.Optional("recipient"): cv.string,
        vol.Optional("description"): vol.All(cv.string, vol.Length(min=1)),
        # Date first, so "2026-10-30" means that day (midday) rather than midnight.
        vol.Optional("date"): vol.Any(cv.date, cv.datetime),
    }
)

REFRESH_SCHEMA = vol.Schema({})


@dataclass
class Target:
    """The group or friend an action is for."""

    coordinator: SplitProCoordinator
    group: Group | None
    friend: Member | None

    @property
    def members(self) -> list[Member]:
        """Who can take part: the group's members, or you and the friend."""
        if self.group is not None:
            return self.group.members
        assert self.friend is not None
        return [self.coordinator.data.me, self.friend]

    @property
    def label(self) -> str:
        return self.group.name if self.group is not None else self.friend.name  # type: ignore[union-attr]


def async_register_services(hass: HomeAssistant) -> None:
    """Register the actions once, for all Split Pro entries."""
    if hass.services.has_service(DOMAIN, SERVICE_ADD_EXPENSE):
        return

    async def add_expense(call: ServiceCall) -> ServiceResponse:
        return await _add_expense(hass, call)

    async def settle_up(call: ServiceCall) -> ServiceResponse:
        return await _settle_up(hass, call)

    async def refresh(call: ServiceCall) -> None:
        for coordinator in _coordinators(hass):
            await coordinator.async_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_ADD_EXPENSE, add_expense, schema=ADD_EXPENSE_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SETTLE_UP, settle_up, schema=SETTLE_UP_SCHEMA, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, refresh, schema=REFRESH_SCHEMA)


async def _add_expense(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    target = _resolve_target(hass, call.data["device"])
    data = target.coordinator.data
    members = target.members
    currency = _currency(call.data.get("currency"), data.fallback_currency(target.group))
    amount = _minor(call.data["amount"], currency)
    paid_by = _member(members, call.data["paid_by"], target) if "paid_by" in call.data else data.me
    split = await _split(call.data, target, amount, currency)

    try:
        participants = participant_amounts(amount, paid_by.id, split)
    except SplitError as err:
        placeholders = dict(err.placeholders)
        for key in ("total", "amount"):  # minor units -> what the user typed
            if key in placeholders:
                placeholders[key] = f"{from_minor_units(int(placeholders[key]), currency):g} {currency}"
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key=err.key, translation_placeholders=placeholders
        ) from err

    expense = {
        "name": call.data["description"],
        "currency": currency,
        "amount": str(amount),
        "paidBy": paid_by.id,
        "groupId": target.group.id if target.group is not None else None,
        "category": call.data["category"],
        "splitType": split.type.value,
        "participants": [{"userId": user_id, "amount": str(value)} for user_id, value in participants.items()],
        "expenseDate": _expense_date(call.data.get("date")),
    }
    created = await _write(target, expense)
    names = {member.id: member.name for member in members}
    return {
        "expense_id": created.get("id"),
        "amount": from_minor_units(amount, currency),
        "currency": currency,
        "paid_by": paid_by.name,
        # Each person's net effect: positive = they're owed, negative = they owe.
        "participants": {
            names.get(user_id, str(user_id)): from_minor_units(value, currency) for user_id, value in participants.items()
        },
    }


async def _settle_up(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    """Record a payment from `payer` (default you) to `recipient`, as Split Pro's settle up does."""
    target = _resolve_target(hass, call.data["device"])
    data = target.coordinator.data
    members = target.members
    currency = _currency(call.data.get("currency"), data.fallback_currency(target.group))
    amount = _minor(call.data["amount"], currency)
    payer = _member(members, call.data["payer"], target) if "payer" in call.data else data.me
    if "recipient" in call.data:
        recipient = _member(members, call.data["recipient"], target)
    elif target.friend is not None:
        recipient = target.friend if payer.id == data.me.id else data.me
    else:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="recipient_required")
    if recipient.id == payer.id:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="settle_same_person")

    expense = {
        "name": call.data.get("description") or f"{payer.name} paid {recipient.name}",
        "currency": currency,
        "amount": str(amount),
        "paidBy": payer.id,
        "groupId": target.group.id if target.group is not None else None,
        "category": DEFAULT_CATEGORY,
        "splitType": "SETTLEMENT",
        "participants": [
            {"userId": payer.id, "amount": str(amount)},
            {"userId": recipient.id, "amount": str(-amount)},
        ],
        "expenseDate": _expense_date(call.data.get("date")),
    }
    created = await _write(target, expense)
    return {
        "expense_id": created.get("id"),
        "amount": from_minor_units(amount, currency),
        "currency": currency,
        "payer": payer.name,
        "recipient": recipient.name,
    }


async def _write(target: Target, expense: dict[str, Any]) -> dict[str, Any]:
    """Send the expense, then refresh balances straight away."""
    try:
        created = await target.coordinator.client.add_expense(expense)
    except SplitProRequestError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="rejected",
            translation_placeholders={"target": target.label, "message": str(err)},
        ) from err
    except SplitProError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="write_failed", translation_placeholders={"message": str(err)}
        ) from err
    await target.coordinator.async_refresh()
    return created or {}


async def _split(data: dict[str, Any], target: Target, amount: int, currency: str) -> Split:
    """The split asked for, defaulting to the group's (or friend's) saved default split, else equal."""
    members = target.members
    mode = data["split"]
    shares = data.get("shares")
    if mode in (SPLIT_PERCENTAGE, SPLIT_SHARES, SPLIT_EXACT) and not shares:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="shares_required", translation_placeholders={"split": mode}
        )
    if mode not in (SPLIT_PERCENTAGE, SPLIT_SHARES, SPLIT_EXACT) and shares:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="shares_not_used")
    if mode != SPLIT_EQUAL and "participants" in data:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="participants_only_equal")

    if mode == SPLIT_EQUAL:
        chosen = [_member(members, who, target) for who in data["participants"]] if "participants" in data else members
        return equal(_unique([member.id for member in chosen]))

    if mode == SPLIT_DEFAULT:
        return await _default_split(target) or equal([member.id for member in members])

    by_member = {_member(members, who, target).id: value for who, value in shares.items()}
    if mode == SPLIT_PERCENTAGE:
        # Split Pro stores percentages in basis points (10000 = 100%).
        return Split(SplitType.PERCENTAGE, {uid: _whole(value * 100, "percentage") for uid, value in by_member.items()})
    if mode == SPLIT_SHARES:
        # Weights only matter relative to each other; scale so 0.5-style shares stay whole numbers.
        return Split(SplitType.SHARE, {uid: _whole(value * 100, "shares") for uid, value in by_member.items()})
    return Split(SplitType.EXACT, {uid: _minor(value, currency) for uid, value in by_member.items()})


async def _default_split(target: Target) -> Split | None:
    """The saved default split for the group or friend, limited to current members."""
    if target.group is not None:
        saved = target.group.default_split
    else:
        assert target.friend is not None
        try:
            friend = await target.coordinator.client.friend(target.friend.id)
        except SplitProError as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="write_failed", translation_placeholders={"message": str(err)}
            ) from err
        saved = (friend or {}).get("defaultSplit")
    if not saved:
        return None
    try:
        split_type = SplitType(saved["splitType"])
        member_ids = {member.id for member in target.members}
        shares = {int(uid): int(share) for uid, share in saved["shares"].items() if int(uid) in member_ids}
    except (KeyError, TypeError, ValueError):
        _LOGGER.warning("Ignoring an unreadable default split for %s: %s", target.label, saved)
        return None
    return Split(split_type, shares) if any(shares.values()) else None


def _resolve_target(hass: HomeAssistant, device_id: str) -> Target:
    """The group or friend behind a Split Pro device."""
    device = dr.async_get(hass).async_get(device_id)
    if device is not None:
        for domain, identifier in device.identifiers:
            if domain != DOMAIN or "_" not in identifier:
                continue
            entry_id, kind, raw_id = identifier.rsplit("_", 2)
            entry: SplitProConfigEntry | None = hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
            coordinator = entry.runtime_data
            if kind == "group" and (group := coordinator.data.groups.get(int(raw_id))) is not None:
                return Target(coordinator, group, None)
            if kind == "friend" and (friend := coordinator.data.friends.get(int(raw_id))) is not None:
                return Target(coordinator, None, friend)
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="unknown_target")


def _member(members: list[Member], who: str, target: Target) -> Member:
    """Find a member by user id, email or name (case-insensitive)."""
    wanted = who.strip().casefold()
    for match in (
        lambda m: str(m.id) == wanted,
        lambda m: (m.email or "").casefold() == wanted,
        lambda m: m.name.casefold() == wanted,
    ):
        found = [member for member in members if match(member)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="ambiguous_member",
                translation_placeholders={"who": who, "target": target.label},
            )
    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="unknown_member",
        translation_placeholders={
            "who": who,
            "target": target.label,
            "members": ", ".join(member.name for member in members),
        },
    )


def _currency(requested: str | None, fallback: str) -> str:
    currency = requested or fallback
    if not is_currency(currency):
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="unknown_currency", translation_placeholders={"currency": currency}
        )
    return currency


def _minor(value: Decimal, currency: str) -> int:
    try:
        return to_minor_units(value, currency)
    except ValueError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="too_many_decimals",
            translation_placeholders={"amount": str(value), "currency": currency},
        ) from err


def _whole(value: Decimal, what: str) -> int:
    if value != value.to_integral_value():
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="too_precise", translation_placeholders={"what": what}
        )
    return int(value)


def _expense_date(value: datetime | date | None) -> str:
    """ISO timestamp with offset; a plain date means midday local time, so it stays on that day."""
    if value is None:
        moment = dt_util.now()
    elif isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())
    else:
        moment = datetime.combine(value, time(12), tzinfo=dt_util.get_default_time_zone())
    return moment.isoformat(timespec="seconds")


def _unique(ids: list[int]) -> list[int]:
    return list(dict.fromkeys(ids))


def _coordinators(hass: HomeAssistant) -> list[SplitProCoordinator]:
    return [
        entry.runtime_data
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
