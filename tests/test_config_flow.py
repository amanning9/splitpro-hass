"""Setup (URL + key, then which sensors), options, reauth and reconfigure."""

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType

from .conftest import API_KEY, URL, setup_entry

USER_INPUT = {"url": URL + "/", "api_key": API_KEY, "verify_ssl": True}


async def test_setup_creates_entry_with_chosen_sensors(hass, fake):
    result = await hass.config_entries.flow.async_init("splitpro", context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "sensors"
    schema = result["data_schema"].schema
    # Archived groups aren't offered; nothing is chosen by default.
    groups = next(v for k, v in schema.items() if k == "group_balances")
    assert [o["label"] for o in groups.config["options"]] == ["House", "Trip"]
    assert {k: k.default() for k in schema} == {"total_balance": False, "group_balances": [], "friend_balances": []}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"total_balance": True, "group_balances": ["10"], "friend_balances": []}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alex (splitpro.test)"
    assert result["data"] == {"url": URL, "api_key": API_KEY, "verify_ssl": True}
    assert result["options"] == {"total_balance": True, "group_balances": ["10"], "friend_balances": []}
    assert result["result"].unique_id == "splitpro.test:1"


async def test_bad_key(hass, fake):
    fake.api_key = "wrong"
    result = await hass.config_entries.flow.async_init("splitpro", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "invalid_auth"}


async def test_not_a_split_pro_api(hass, fake):
    fake.down = True
    result = await hass.config_entries.flow.async_init("splitpro", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_url_needs_scheme(hass, fake):
    result = await hass.config_entries.flow.async_init("splitpro", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, "url": "splitpro.test"})
    assert result["errors"] == {"url": "invalid_url"}


async def test_same_account_twice(hass, fake):
    await setup_entry(hass)
    result = await hass.config_entries.flow.async_init("splitpro", context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_change_sensors(hass, fake):
    entry = await setup_entry(hass, total_balance=True, group_balances=["10"])
    assert hass.states.get("sensor.house_balance") is not None

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["step_id"] == "init"
    defaults = {k: k.default() for k in result["data_schema"].schema}
    assert defaults == {"total_balance": True, "group_balances": ["10"], "friend_balances": []}

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"total_balance": False, "group_balances": [], "friend_balances": ["2"]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Reloaded: deselected sensors are gone (from the registry too), the new one exists.
    assert hass.states.get("sensor.house_balance") is None
    assert hass.states.get("sensor.alex_splitpro_test_total_balance") is None
    assert hass.states.get("sensor.sam_balance") is not None


async def test_reauth(hass, fake):
    entry = await setup_entry(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": "wrong"})
    # The fake only checks a flag, so a wrong key still works here; the point is the update.
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data["api_key"] == "wrong"


async def test_reauth_wrong_account(hass, fake):
    entry = await setup_entry(hass)
    fake.me = {**fake.me, "id": 99}
    result = await entry.start_reauth_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"api_key": API_KEY})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "wrong_account"


async def test_reconfigure_keeps_account(hass, fake):
    entry = await setup_entry(hass)
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**USER_INPUT, "verify_ssl": False})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data["verify_ssl"] is False

    fake.me = {**fake.me, "id": 99}
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["reason"] == "wrong_account"
