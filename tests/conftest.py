"""Run from the repo root: pip install -r requirements_test.txt; pytest tests

`fake` is a stand-in Split Pro serving the same JSON shapes as the real REST API (tRPC envelopes,
amounts in minor units), so tests drive the integration through aioclient_mock.
"""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any
from urllib.parse import parse_qs

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

import custom_components

# The test plugin's own custom_components package is imported first; add ours to its path.
custom_components.__path__[:0] = [str(pathlib.Path(__file__).parent.parent / "custom_components")]

pytest_plugins = ["pytest_homeassistant_custom_component"]

URL = "https://splitpro.test"
API_KEY = "test-secret"

ALEX = {"id": 1, "name": "Alex", "email": "alex@example.com"}
SAM = {"id": 2, "name": "Sam", "email": "sam@example.com"}
JO = {"id": 3, "name": "Jo", "email": "jo@example.com"}
KIM = {"id": 4, "name": "Kim", "email": "kim@example.com"}


class FakeSplitPro:
    """Split Pro's REST API with three groups and three friends; tweak attributes per test."""

    def __init__(self) -> None:
        self.api_key = API_KEY
        self.me = {**ALEX, "image": "", "currency": "GBP", "defaultCurrency": None}
        self.groups = [
            _group(10, "House", [ALEX, SAM, JO], default_currency="GBP"),
            _group(11, "Old flat", [ALEX, SAM], archived=True),
            _group(12, "Trip", [ALEX, SAM], default_currency="EUR",
                   default_split={"splitType": "SHARE", "shares": {"1": "200", "2": "100"}}),
        ]
        self.friends = [SAM, JO, KIM]
        self.friend_default_splits: dict[int, Any] = {}
        # expense.getBalances: friend id -> {currency: amount}; positive = they owe Alex.
        self.balances: dict[int, dict[str, int]] = {2: {"GBP": 1500}, 3: {"GBP": -500}}
        # group.getGroupDetails balance rows: (userId, friendId, currency, amount), both directions.
        self.group_balances: dict[int, list[tuple[int, int, str, int]]] = {
            10: [(1, 2, "GBP", 1000), (2, 1, "GBP", -1000), (1, 3, "GBP", -250), (3, 1, "GBP", 250)],
            12: [(1, 2, "EUR", 300), (2, 1, "EUR", -300), (1, 2, "GBP", 100), (2, 1, "GBP", -100)],
        }
        self.posted: list[Any] = []  # bodies POSTed to expense.addOrEditExpense
        self.calls: list[str] = []
        self.reject: tuple[int, str] | None = None  # make the next POST fail with (status, message)
        self.down = False

    async def handle(self, method: str, url: Any, data: Any) -> AiohttpClientMockResponse:
        procedure = url.path.rsplit("/", 1)[-1]
        self.calls.append(procedure)
        if self.down:
            return AiohttpClientMockResponse(method, url, status=502, text="Bad gateway")
        params = parse_qs(url.query_string)
        inp = json.loads(params["input"][0]) if "input" in params else {}

        # The real handler 401s every procedure without a valid key (headers aren't passed to
        # side effects, so the key is checked with a flag).
        if self.api_key != API_KEY:
            return _error(method, url, 401, "UNAUTHORIZED", "UNAUTHORIZED")
        if "POST" == method.upper():
            assert procedure == "expense.addOrEditExpense"
            self.posted.append(data)
            if self.reject:
                status, message = self.reject
                self.reject = None
                return _error(method, url, status, {403: "FORBIDDEN", 400: "BAD_REQUEST"}[status], message)
            return _ok(method, url, [{"id": f"expense-{len(self.posted)}", **data[0]}])
        if procedure == "user.me":
            return _ok(method, url, self.me)
        if procedure == "user.getFriends":
            return _ok(method, url, self.friends)
        if procedure == "user.getFriend":
            friend = next((f for f in self.friends if f["id"] == inp["friendId"]), None)
            return _ok(method, url, friend and {**friend, "defaultSplit": self.friend_default_splits.get(friend["id"])})
        if procedure == "group.getAllGroups":
            return _ok(method, url, [{"groupId": g["id"], "userId": 1, "group": g} for g in self.groups])
        if procedure == "group.getGroupDetails":
            group = next(g for g in self.groups if g["id"] == inp["groupId"])
            rows = [
                {"userId": u, "friendId": f, "groupId": group["id"], "currency": c, "amount": a}
                for u, f, c, a in self.group_balances.get(group["id"], [])
            ]
            return _ok(method, url, {**group, "groupBalances": rows})
        if procedure == "expense.getBalances":
            return _ok(method, url, {"balances": [
                {"friendId": fid, "currencies": [{"currency": c, "amount": a} for c, a in b.items()],
                 "friend": next(f for f in self.friends if f["id"] == fid)}
                for fid, b in self.balances.items()
            ]})
        raise AssertionError(f"Unexpected Split Pro call {method} {procedure}")


def _group(gid, name, members, default_currency=None, default_split=None, archived=False):
    return {
        "id": gid, "name": name, "defaultCurrency": default_currency, "defaultSplit": default_split,
        "archivedAt": "2026-01-01T00:00:00.000Z" if archived else None,
        "groupUsers": [{"groupId": gid, "userId": m["id"], "user": m} for m in members],
    }


def _ok(method, url, data):
    return AiohttpClientMockResponse(method, url, json={"result": {"data": data}})


def _error(method, url, status, code, message):
    return AiohttpClientMockResponse(
        method, url, status=status,
        json={"error": {"message": message, "code": -32600, "data": {"code": code, "httpStatus": status}}},
    )


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def fake(aioclient_mock) -> FakeSplitPro:
    api = FakeSplitPro()
    pattern = re.compile(re.escape(URL) + r"/api/v1/.*")
    aioclient_mock.get(pattern, side_effect=api.handle)
    aioclient_mock.post(pattern, side_effect=api.handle)
    return api


def make_entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain="splitpro",
        title="Alex (splitpro.test)",
        unique_id="splitpro.test:1",
        data={"url": URL, "api_key": API_KEY, "verify_ssl": True},
        options={"total_balance": False, "group_balances": [], "friend_balances": [], **options},
    )


async def setup_entry(hass, **options: Any) -> MockConfigEntry:
    entry = make_entry(**options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
