"""Constants for the Split Pro integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "splitpro"

CONF_VERIFY_SSL = "verify_ssl"

# Options: which balance sensors exist (all off by default).
CONF_TOTAL = "total_balance"
CONF_GROUPS = "group_balances"
CONF_FRIENDS = "friend_balances"

UPDATE_INTERVAL = timedelta(minutes=5)

SERVICE_ADD_EXPENSE = "add_expense"
SERVICE_SETTLE_UP = "settle_up"
SERVICE_REFRESH = "refresh"
