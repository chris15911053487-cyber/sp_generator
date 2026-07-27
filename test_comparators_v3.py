from app.contracts.reference import ComparatorSpec
from app.services.comparators_v3 import compare_rows


def test_keyed_rows_reports_missing_extra_value_diff_and_duplicate():
    spec = ComparatorSpec(
        type="keyed_rows_equal",
        key_columns=["id"],
        compare_columns=["amount"],
        tolerance={"amount": 0.01},
    )
    result = compare_rows(
        "income",
        spec,
        [
            {"id": 1, "amount": 10},
            {"id": 1, "amount": 10},
            {"id": 2, "amount": 22},
            {"id": 4, "amount": 40},
        ],
        [
            {"id": 1, "amount": 10},
            {"id": 2, "amount": 20},
            {"id": 3, "amount": 30},
        ],
    )
    assert not result.match
    assert result.missing[0]["key"] == [3]
    assert result.extra[0]["key"] == [4]
    assert result.differences[0]["key"] == [2]
    assert result.duplicate_keys[0]["key"] == [1]


def test_money_tolerance_is_honored():
    spec = ComparatorSpec(
        type="scalar_metrics_equal",
        compare_columns=["amount"],
        tolerance={"amount": 0.01},
    )
    result = compare_rows(
        "total",
        spec,
        [{"amount": 100.005}],
        [{"amount": 100.0}],
    )
    assert result.match


def test_difference_evidence_is_bounded_but_total_is_preserved():
    spec = ComparatorSpec(
        type="keyed_rows_equal",
        key_columns=["id"],
        compare_columns=["amount"],
    )
    result = compare_rows(
        "large",
        spec,
        [],
        [{"id": index, "amount": index} for index in range(250)],
    )
    assert len(result.missing) == 200
    assert result.difference_totals["missing"] == 250
