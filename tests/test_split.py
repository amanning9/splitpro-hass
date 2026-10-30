"""The split maths (a port of Split Pro's calculateParticipantSplit) and money conversions."""

from decimal import Decimal

import pytest

from custom_components.splitpro.money import decimal_digits, from_minor_units, is_currency, to_minor_units
from custom_components.splitpro.split import Split, SplitError, SplitType, equal, participant_amounts

ALEX, SAM, JO = 1, 2, 3


def test_equal_split_three_ways_gives_extra_penny_to_a_non_payer():
    # £10.00 three ways: 333 each, one penny over, which Split Pro gives a non-payer.
    result = participant_amounts(1000, ALEX, equal([ALEX, SAM, JO]))
    assert result == {ALEX: 667, SAM: -334, JO: -333}
    assert sum(result.values()) == 0


def test_equal_split_payer_not_included():
    # Alex buys something for Sam and Jo only.
    assert participant_amounts(1000, ALEX, equal([SAM, JO])) == {ALEX: 1000, SAM: -500, JO: -500}


def test_split_where_no_one_owes_the_payer_is_rejected():
    # Only the payer is included, so the expense would change no balances.
    with pytest.raises(SplitError) as err:
        participant_amounts(1000, ALEX, Split(SplitType.EQUAL, {ALEX: 1, SAM: 0}))
    assert err.value.key == "nothing_owed"


def test_equal_split_payer_is_someone_else():
    assert participant_amounts(900, SAM, equal([ALEX, SAM, JO])) == {ALEX: -300, SAM: 600, JO: -300}


def test_percentage_split_in_basis_points():
    # 60% of £10.01 is 600p and 40% is 400p (rounded down); the leftover penny goes to the first
    # person with a non-zero amount, here the payer.
    assert participant_amounts(1001, ALEX, Split(SplitType.PERCENTAGE, {ALEX: 6000, SAM: 4000})) == {
        ALEX: 400,
        SAM: -400,
    }


def test_percentages_must_total_100():
    with pytest.raises(SplitError) as err:
        participant_amounts(1000, ALEX, Split(SplitType.PERCENTAGE, {ALEX: 5000, SAM: 4000}))
    assert err.value.key == "percentages_not_100"
    assert err.value.placeholders == {"total": "90"}


def test_shares_split():
    assert participant_amounts(3000, ALEX, Split(SplitType.SHARE, {ALEX: 200, SAM: 100})) == {ALEX: 1000, SAM: -1000}


def test_shares_split_rounding_sums_to_zero():
    result = participant_amounts(1000, JO, Split(SplitType.SHARE, {ALEX: 1, SAM: 1, JO: 1}))
    assert sum(result.values()) == 0
    assert sorted(result.values()) == [-334, -333, 667]


def test_exact_split_must_match_amount():
    assert participant_amounts(1000, ALEX, Split(SplitType.EXACT, {ALEX: 250, SAM: 750})) == {ALEX: 750, SAM: -750}
    with pytest.raises(SplitError) as err:
        participant_amounts(1000, ALEX, Split(SplitType.EXACT, {ALEX: 250, SAM: 700}))
    assert err.value.key == "exact_not_total"
    assert err.value.placeholders == {"total": "950", "amount": "1000"}


@pytest.mark.parametrize("amount", [0, -100])
def test_amount_must_be_positive(amount):
    with pytest.raises(SplitError) as err:
        participant_amounts(amount, ALEX, equal([ALEX, SAM]))
    assert err.value.key == "amount_not_positive"


def test_no_one_included():
    with pytest.raises(SplitError) as err:
        participant_amounts(1000, ALEX, Split(SplitType.SHARE, {ALEX: 0, SAM: 0}))
    assert err.value.key == "no_participants"


@pytest.mark.parametrize(
    ("amount", "currency", "minor"),
    [("12.34", "GBP", 1234), ("12", "GBP", 1200), ("1500", "JPY", 1500), ("1.234", "KWD", 1234), (Decimal("0.5"), "EUR", 50)],
)
def test_to_minor_units(amount, currency, minor):
    assert to_minor_units(amount, currency) == minor
    assert from_minor_units(minor, currency) == float(Decimal(str(amount)))


def test_too_many_decimals():
    with pytest.raises(ValueError):
        to_minor_units("12.345", "GBP")
    with pytest.raises(ValueError):
        to_minor_units("1.5", "JPY")


def test_currencies():
    assert is_currency("GBP") and is_currency("JPY") and not is_currency("XYZ") and not is_currency("constructor")
    assert (decimal_digits("GBP"), decimal_digits("JPY"), decimal_digits("BHD")) == (2, 0, 3)
