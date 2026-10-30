"""Balance sensors exist only when chosen; values, currencies, and devices for every group/friend."""

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .conftest import setup_entry


def _sensors(hass, entry):
    return sorted(e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id))


async def test_no_sensors_by_default_but_devices_for_everyone(hass, fake):
    entry = await setup_entry(hass)
    assert _sensors(hass, entry) == []
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    # Account, two active groups (not the archived one) and three friends.
    assert sorted(d.name for d in devices) == ["Alex (splitpro.test)", "House", "Jo", "Kim", "Sam", "Trip"]
    # Only what's needed: no group details fetched without group sensors.
    assert "group.getGroupDetails" not in fake.calls


async def test_chosen_sensors(hass, fake):
    entry = await setup_entry(hass, total_balance=True, group_balances=["10"], friend_balances=["2", "3"])
    assert _sensors(hass, entry) == [
        "sensor.alex_splitpro_test_total_balance",
        "sensor.house_balance",
        "sensor.jo_balance",
        "sensor.sam_balance",
    ]

    total = hass.states.get("sensor.alex_splitpro_test_total_balance")
    assert (total.state, total.attributes["unit_of_measurement"]) == ("10.0", "GBP")  # 15.00 - 5.00
    assert total.attributes["device_class"] == "monetary"

    house = hass.states.get("sensor.house_balance")
    assert (house.state, house.attributes["unit_of_measurement"]) == ("7.5", "GBP")  # Sam owes 10, Alex owes Jo 2.50
    assert house.attributes["group_id"] == 10
    assert house.attributes["members"] == {"Alex": {"GBP": 7.5}, "Sam": {"GBP": -10.0}, "Jo": {"GBP": 2.5}}

    assert hass.states.get("sensor.sam_balance").state == "15.0"
    assert hass.states.get("sensor.jo_balance").state == "-5.0"


async def test_more_than_one_currency_is_unavailable(hass, fake):
    await setup_entry(hass, total_balance=True, group_balances=["12"], friend_balances=["3"])
    fake.balances[3] = {"GBP": -500, "EUR": 300}
    await hass.services.async_call("splitpro", "refresh", blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.trip_balance").state == STATE_UNAVAILABLE  # EUR and GBP
    assert hass.states.get("sensor.jo_balance").state == STATE_UNAVAILABLE
    assert hass.states.get("sensor.alex_splitpro_test_total_balance").state == STATE_UNAVAILABLE

    # Back to one currency: available again.
    fake.balances[3] = {"EUR": 300}
    await hass.services.async_call("splitpro", "refresh", blocking=True)
    await hass.async_block_till_done()
    jo = hass.states.get("sensor.jo_balance")
    assert (jo.state, jo.attributes["unit_of_measurement"]) == ("3.0", "EUR")


async def test_zero_balance_uses_default_currency(hass, fake):
    fake.group_balances[12] = []
    fake.me["defaultCurrency"] = "USD"
    await setup_entry(hass, group_balances=["12"], friend_balances=["4"])
    trip = hass.states.get("sensor.trip_balance")
    assert (trip.state, trip.attributes["unit_of_measurement"]) == ("0.0", "EUR")  # the group's default
    kim = hass.states.get("sensor.kim_balance")
    assert (kim.state, kim.attributes["unit_of_measurement"]) == ("0.0", "USD")  # yours


async def test_departed_group_and_friend_are_removed(hass, fake):
    entry = await setup_entry(hass, group_balances=["10"], friend_balances=["4"])
    fake.groups = [g for g in fake.groups if g["id"] != 10]
    fake.friends = [f for f in fake.friends if f["id"] != 4]
    await hass.services.async_call("splitpro", "refresh", blocking=True)
    await hass.async_block_till_done()
    # Their devices go, and the sensors with them (they come back if they reappear and you reload).
    assert hass.states.get("sensor.house_balance") is None
    assert hass.states.get("sensor.kim_balance") is None
    names = sorted(d.name for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id))
    assert names == ["Alex (splitpro.test)", "Jo", "Sam", "Trip"]


async def test_split_pro_down_makes_sensors_unavailable(hass, fake):
    await setup_entry(hass, friend_balances=["2"])
    fake.down = True
    await hass.services.async_call("splitpro", "refresh", blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.sam_balance").state == STATE_UNAVAILABLE
