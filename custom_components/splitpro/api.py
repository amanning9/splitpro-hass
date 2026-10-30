"""Client for Split Pro's REST API (/api/v1, alex-hacks build or upstream once released).

Every response is a tRPC envelope: {"result": {"data": ...}} on success, and
{"error": {"message", "data": {"code", "httpStatus", "zodError"}}} on failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import aiohttp

_LOGGER = logging.getLogger(__name__)

TIMEOUT = aiohttp.ClientTimeout(total=30)


class SplitProError(Exception):
    """Split Pro couldn't be reached or returned an error."""


class SplitProAuthError(SplitProError):
    """The API key was missing or wrong (HTTP 401 on user.me)."""


class SplitProRequestError(SplitProError):
    """Split Pro refused the request (400/401/403), e.g. not a group member or archived group."""

    def __init__(self, message: str, status: int, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class SplitProClient:
    """Thin async wrapper around the endpoints the integration uses."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str, verify_ssl: bool = True) -> None:
        self._session = session
        self._base = url.rstrip("/") + "/api/v1/"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._ssl = None if verify_ssl else False

    async def me(self) -> dict[str, Any]:
        """The API key's user. Raises SplitProAuthError for a bad key."""
        try:
            return await self._get("user.me")
        except SplitProRequestError as err:
            if 401 == err.status:
                raise SplitProAuthError(str(err)) from err
            raise

    async def friends(self) -> list[dict[str, Any]]:
        return await self._get("user.getFriends")

    async def friend(self, friend_id: int) -> dict[str, Any] | None:
        return await self._get("user.getFriend", friendId=friend_id)

    async def groups(self) -> list[dict[str, Any]]:
        """Every group you're in, archived ones included (check group["archivedAt"])."""
        return [membership["group"] for membership in await self._get("group.getAllGroups")]

    async def group_details(self, group_id: int) -> dict[str, Any]:
        """A group with groupUsers, groupBalances (simplified if the group simplifies debts) and defaultSplit."""
        return await self._get("group.getGroupDetails", groupId=group_id)

    async def balances(self) -> list[dict[str, Any]]:
        """Your balance with each friend: [{friendId, currencies: [{currency, amount}]}]. Positive = owed to you."""
        return (await self._get("expense.getBalances"))["balances"]

    async def add_expense(self, expense: dict[str, Any]) -> dict[str, Any]:
        """Create one expense; returns the stored expense (with its id)."""
        result = await self._post("expense.addOrEditExpense", [expense])
        return result[0]

    async def _get(self, procedure: str, **params: Any) -> Any:
        query = {"input": json.dumps(params)} if params else None
        return await self._request("GET", procedure, params=query)

    async def _post(self, procedure: str, body: Any) -> Any:
        return await self._request("POST", procedure, json=body)

    async def _request(self, method: str, procedure: str, **kwargs: Any) -> Any:
        try:
            async with self._session.request(
                method, self._base + procedure, headers=self._headers, ssl=self._ssl, timeout=TIMEOUT, **kwargs
            ) as resp:
                try:
                    payload = await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as err:
                    raise SplitProError(
                        f"Split Pro returned HTTP {resp.status} without JSON for {procedure}. "
                        "Is the URL right, and is it a build with the REST API?"
                    ) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SplitProError(f"Couldn't reach Split Pro: {err}") from err

        if not isinstance(payload, dict):
            raise SplitProError(f"Unexpected response from Split Pro for {procedure}")
        if "error" in payload:
            error = payload["error"]
            data = error.get("data") or {}
            status = int(data.get("httpStatus") or resp.status)
            message = _error_message(error)
            _LOGGER.debug("Split Pro %s %s failed (%s): %s", method, procedure, status, message)
            if status in (400, 401, 403):
                raise SplitProRequestError(message, status, data.get("code"))
            raise SplitProError(f"Split Pro error {status} for {procedure}: {message}")
        if "result" not in payload:
            raise SplitProError(f"Unexpected response from Split Pro for {procedure}")
        return payload["result"].get("data")


def _error_message(error: dict[str, Any]) -> str:
    """The error message, with field errors from validation failures if there are any."""
    message = str(error.get("message") or "Unknown error")
    zod = (error.get("data") or {}).get("zodError") or {}
    fields = [f"{field}: {', '.join(errors)}" for field, errors in (zod.get("fieldErrors") or {}).items() if errors]
    return f"{message} ({'; '.join(fields)})" if fields else message
