"""add_expense, settle_up and refresh: the payload Split Pro receives, defaults, and errors."""

import json
import pathlib

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .conftest import setup_entry

BASE = pathlib.Path(__file__).parent.parent / "custom_components/splitpro"


def _device(hass, entry, kind, object_id):
    device = dr.async_get(hass).async_get_device(identifiers={("splitpro", f"{entry.entry_id}_{kind}_{object_id}")})
    assert device is not None
    return device.id


async def _add(hass, device_id, **data):
    return await hass.services.async_call(
        "splitpro", "add_expense", {"device": device_id, **data}, blocking=True, return_response=True
    )


def _participants(fake):
    return {p["userId"]: int(p["amount"]) for p in fake.posted[-1][0]["participants"]}


async def test_add_group_expense_equal_by_default(hass, fake):
    entry = await setup_entry(hass, group_balances=["10"])
    house = _device(hass, entry, "group", 10)
    fake.calls.clear()

    response = await _add(hass, house, description="Groceries", amount=30, category="groceries", date="2026-10-30")

    (expense,) = fake.posted[-1]  # one expense, sent as a JSON array
    assert expense == {
        "name": "Groceries",
        "currency": "GBP",  # the group's default
        "amount": "3000",
        "paidBy": 1,
        "groupId": 10,
        "category": "groceries",
        "splitType": "EQUAL",
        "participants": [
            {"userId": 1, "amount": "2000"},
            {"userId": 2, "amount": "-1000"},
            {"userId": 3, "amount": "-1000"},
        ],
        "expenseDate": expense["expenseDate"],
    }
    assert expense["expenseDate"].startswith("2026-10-30T12:00:00")  # midday local, with an offset
    assert response == {
        "expense_id": "expense-1",
        "amount": 30.0,
        "currency": "GBP",
        "paid_by": "Alex",
        "participants": {"Alex": 20.0, "Sam": -10.0, "Jo": -10.0},
    }
    # Balances refreshed straight after the write.
    assert "group.getGroupDetails" in fake.calls[fake.calls.index("expense.addOrEditExpense"):]


async def test_group_default_split_is_used(hass, fake):
    entry = await setup_entry(hass)
    # Trip's saved default: Alex 2 shares, Sam 1; currency EUR.
    await _add(hass, _device(hass, entry, "group", 12), description="Dinner", amount="90.00")
    expense = fake.posted[-1][0]
    assert (expense["splitType"], expense["currency"]) == ("SHARE", "EUR")
    assert _participants(fake) == {1: 3000, 2: -3000}


async def test_paid_by_someone_else(hass, fake):
    entry = await setup_entry(hass)
    response = await _add(hass, _device(hass, entry, "group", 10), description="Milk", amount=3, paid_by="sam@example.com")
    assert fake.posted[-1][0]["paidBy"] == 2
    assert _participants(fake) == {1: -100, 2: 200, 3: -100}
    assert response["paid_by"] == "Sam"


async def test_equal_split_between_some_members(hass, fake):
    entry = await setup_entry(hass)
    await _add(hass, _device(hass, entry, "group", 10), description="Taxi", amount=10, split="equal", participants=["Jo"])
    # Alex paid for Jo alone.
    assert _participants(fake) == {3: -1000, 1: 1000}


@pytest.mark.parametrize(
    ("split", "shares", "expected_type", "expected"),
    [
        ("percentage", {"Alex": 60, "Sam": 40}, "PERCENTAGE", {1: 400, 2: -400}),
        ("shares", {"Alex": 1, "Sam": 0.5, "Jo": 0.5}, "SHARE", {1: 500, 2: -250, 3: -250}),
        ("exact", {"Alex": 2.5, "Sam": 7.5}, "EXACT", {1: 750, 2: -750}),
    ],
)
async def test_explicit_splits(hass, fake, split, shares, expected_type, expected):
    entry = await setup_entry(hass)
    await _add(hass, _device(hass, entry, "group", 10), description="Bill", amount=10, split=split, shares=shares)
    assert fake.posted[-1][0]["splitType"] == expected_type
    assert _participants(fake) == expected


async def test_friend_expense(hass, fake):
    entry = await setup_entry(hass)
    sam = _device(hass, entry, "friend", 2)
    await _add(hass, sam, description="Coffee", amount=5)
    expense = fake.posted[-1][0]
    assert (expense["groupId"], expense["currency"]) == (None, "GBP")
    assert _participants(fake) == {1: 250, 2: -250}

    # A friend's saved default split is used too.
    fake.friend_default_splits[2] = {"splitType": "PERCENTAGE", "shares": {"1": "0", "2": "10000"}}
    await _add(hass, sam, description="Sam's ticket", amount=12)
    assert fake.posted[-1][0]["splitType"] == "PERCENTAGE"
    assert _participants(fake) == {1: 1200, 2: -1200}


@pytest.mark.parametrize(
    ("data", "key"),
    [
        ({"paid_by": "Nobody"}, "unknown_member"),
        ({"currency": "XYZ"}, "unknown_currency"),
        ({"amount": "1.234"}, "too_many_decimals"),
        ({"split": "exact", "shares": {"Alex": 5}}, "exact_not_total"),
        ({"split": "percentage", "shares": {"Alex": 50}}, "percentages_not_100"),
        ({"split": "shares"}, "shares_required"),
        ({"shares": {"Alex": 1}}, "shares_not_used"),
        ({"split": "percentage", "participants": ["Sam"], "shares": {"Sam": 100}}, "participants_only_equal"),
    ],
)
async def test_bad_input(hass, fake, data, key):
    entry = await setup_entry(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _add(hass, _device(hass, entry, "group", 10), **{"description": "Bill", "amount": 10, **data})
    assert err.value.translation_key == key
    assert fake.posted == []  # nothing sent


async def test_exact_error_shows_main_units(hass, fake):
    entry = await setup_entry(hass)
    with pytest.raises(ServiceValidationError) as err:
        await _add(hass, _device(hass, entry, "group", 10), description="x", amount=10, split="exact", shares={"Alex": 5})
    assert err.value.translation_placeholders == {"total": "5 GBP", "amount": "10 GBP"}


async def test_target_must_be_a_group_or_friend(hass, fake):
    entry = await setup_entry(hass)
    account = dr.async_get(hass).async_get_device(identifiers={("splitpro", entry.entry_id)})
    with pytest.raises(ServiceValidationError) as err:
        await _add(hass, account.id, description="x", amount=1)
    assert err.value.translation_key == "unknown_target"


async def test_split_pro_refuses(hass, fake):
    entry = await setup_entry(hass)
    fake.reject = (403, "Not a group member")
    with pytest.raises(ServiceValidationError) as err:
        await _add(hass, _device(hass, entry, "group", 10), description="x", amount=1)
    assert err.value.translation_key == "rejected"
    assert err.value.translation_placeholders == {"target": "House", "message": "Not a group member"}


async def test_split_pro_down(hass, fake):
    entry = await setup_entry(hass)
    house = _device(hass, entry, "group", 10)
    fake.down = True
    with pytest.raises(HomeAssistantError) as err:
        await _add(hass, house, description="x", amount=1)
    assert err.value.translation_key == "write_failed"


async def test_settle_up(hass, fake):
    entry = await setup_entry(hass)
    response = await hass.services.async_call(
        "splitpro", "settle_up", {"device": _device(hass, entry, "friend", 2), "amount": 15},
        blocking=True, return_response=True,
    )
    expense = fake.posted[-1][0]
    assert (expense["splitType"], expense["paidBy"], expense["groupId"]) == ("SETTLEMENT", 1, None)
    assert expense["participants"] == [{"userId": 1, "amount": "1500"}, {"userId": 2, "amount": "-1500"}]
    assert response["recipient"] == "Sam"

    # In a group, the recipient has to be named.
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "splitpro", "settle_up", {"device": _device(hass, entry, "group", 10), "amount": 5}, blocking=True
        )
    assert err.value.translation_key == "recipient_required"


def test_services_yaml_matches_strings():
    from homeassistant.helpers.selector import selector
    from homeassistant.util.yaml import load_yaml

    services = load_yaml(str(BASE / "services.yaml"))
    strings = json.loads((BASE / "strings.json").read_text())
    assert strings == json.loads((BASE / "translations/en.json").read_text())
    assert set(services) == set(strings["services"])
    for name, desc in services.items():
        fields = (desc or {}).get("fields", {})
        assert set(fields) == set(strings["services"][name].get("fields", {}))
        for field in fields.values():
            selector(field["selector"])  # raises if the selector config is invalid
    categories = services["add_expense"]["fields"]["category"]["selector"]["select"]["options"]
    assert set(categories) == set(strings["selector"]["category"]["options"])


async def test_date_and_time_are_kept(hass, fake):
    entry = await setup_entry(hass)
    await _add(hass, _device(hass, entry, "group", 10), description="Pub", amount=9, date="2026-10-29 21:15:00")
    assert fake.posted[-1][0]["expenseDate"].startswith("2026-10-29T21:15:00")


async def test_float_amounts_are_taken_as_typed(hass, fake):
    # Number selectors and templates pass floats; 4.66 must not become 4.66000000000000014...
    entry = await setup_entry(hass)
    await _add(hass, _device(hass, entry, "group", 10), description="x", amount=4.66, split="exact",
               shares={"Alex": 0.1, "Sam": 4.56})
    assert fake.posted[-1][0]["amount"] == "466"
    assert _participants(fake) == {1: 456, 2: -456}
    await hass.services.async_call(
        "splitpro", "settle_up", {"device": _device(hass, entry, "friend", 2), "amount": 0.3}, blocking=True
    )
    assert fake.posted[-1][0]["amount"] == "30"


async def test_category_options_map_to_split_pro_values(hass, fake):
    entry = await setup_entry(hass)
    await _add(hass, _device(hass, entry, "group", 10), description="Curry", amount=20, category="dining_out")
    assert fake.posted[-1][0]["category"] == "diningOut"
    await _add(hass, _device(hass, entry, "group", 10), description="Curry", amount=20, category="diningOut")
    assert fake.posted[-1][0]["category"] == "diningOut"
