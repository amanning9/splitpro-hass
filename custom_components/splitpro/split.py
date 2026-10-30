"""Work out each participant's amount for a Split Pro expense.

A port of calculateParticipantSplit in Split Pro's src/store/addStore.ts. The server stores
whatever participant amounts it's sent and checks none of this, so the maths has to be right here.

Split Pro's convention: each participant's amount is what they paid minus their share. The payer's
entry is (expense amount - their own share) and everyone else's is (-their share), so the entries
sum to zero. Amounts are integers in the currency's smallest unit.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SplitType(StrEnum):
    """Split Pro split types the integration can create."""

    EQUAL = "EQUAL"
    PERCENTAGE = "PERCENTAGE"  # shares in basis points: 10000 = 100%
    SHARE = "SHARE"  # shares are relative weights
    EXACT = "EXACT"  # shares are amounts in minor units, summing to the expense amount


class SplitError(ValueError):
    """The split can't be made. `key` is the translation key for the user-facing error."""

    def __init__(self, key: str, **placeholders: str) -> None:
        super().__init__(key)
        self.key = key
        self.placeholders = placeholders


@dataclass(frozen=True)
class Split:
    """How an expense is divided."""

    type: SplitType
    # For EQUAL: who's included (any non-zero value); for the others: each person's share.
    shares: dict[int, int]


def equal(user_ids: list[int]) -> Split:
    """Everyone listed pays the same."""
    return Split(SplitType.EQUAL, {user_id: 1 for user_id in user_ids})


def participant_amounts(amount: int, paid_by: int, split: Split) -> dict[int, int]:
    """Return {user_id: amount} in Split Pro's convention, including the payer.

    Order matters only for leftover pennies, which go one at a time to participants in the order
    of `split.shares` (Split Pro shuffles them pseudo-randomly; the server accepts any order).
    """
    if amount <= 0:
        raise SplitError("amount_not_positive")
    shares = {user_id: share for user_id, share in split.shares.items()}
    if any(share < 0 for share in shares.values()):
        raise SplitError("negative_share")

    owed = _shares_owed(amount, split.type, shares)

    # Everyone pays nothing except the payer, who pays the whole amount (the payer needn't owe a
    # share themselves, e.g. buying something for others).
    result = {user_id: -share for user_id, share in owed.items()}
    result[paid_by] = result.get(paid_by, 0) + amount

    # Integer division leaves a few pennies over: hand them to people other than the payer
    # (for EQUAL), or anyone with a non-zero amount (otherwise), as Split Pro does.
    pennies_left = sum(result.values())
    if split.type is SplitType.EQUAL:
        pick = [user_id for user_id, share in shares.items() if share and user_id != paid_by]
    else:
        pick = [user_id for user_id, value in result.items() if value]
    step = 1 if pennies_left > 0 else -1
    i = 0
    while pennies_left and pick:
        result[pick[i % len(pick)]] -= step
        pennies_left -= step
        i += 1

    if len(result) > 1 and not any(result.values()):
        raise SplitError("nothing_owed")
    return result


def _shares_owed(amount: int, split_type: SplitType, shares: dict[int, int]) -> dict[int, int]:
    """Each participant's share of the amount, before the payer and leftover pennies."""
    if split_type is SplitType.EQUAL:
        included = [user_id for user_id, share in shares.items() if share]
        if not included:
            raise SplitError("no_participants")
        each = amount // len(included)
        return {user_id: each if share else 0 for user_id, share in shares.items()}

    if split_type is SplitType.PERCENTAGE:
        if sum(shares.values()) != 10000:
            raise SplitError("percentages_not_100", total=_percent(sum(shares.values())))
        return {user_id: share * amount // 10000 for user_id, share in shares.items()}

    if split_type is SplitType.SHARE:
        total = sum(shares.values())
        if not total:
            raise SplitError("no_participants")
        return {user_id: share * amount // total for user_id, share in shares.items()}

    if split_type is SplitType.EXACT:
        total = sum(shares.values())
        if total != amount:
            raise SplitError("exact_not_total", total=str(total), amount=str(amount))
        return dict(shares)

    raise SplitError("unsupported_split", split=str(split_type))


def _percent(basis_points: int) -> str:
    return f"{basis_points / 100:g}"
