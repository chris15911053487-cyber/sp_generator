"""结果级比较器：比较数据，不比较 SQL 文本或查询过程。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.contracts.reference import ComparatorSpec
from app.contracts.validation import ComparisonEvidence


class ComparisonError(ValueError):
    pass


MAX_DIFFERENCE_ITEMS = 200


def _column_map(row: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in row:
        key = str(name).casefold()
        if key in result:
            raise ComparisonError(f"结果包含仅大小写不同的重复列: {name}")
        result[key] = str(name)
    return result


def _value(row: dict, column: str) -> Any:
    names = _column_map(row)
    actual = names.get(column.casefold())
    if actual is None:
        raise ComparisonError(f"结果缺少比较列: {column}")
    return row[actual]


def _equal(left: Any, right: Any, tolerance: float | None) -> bool:
    if left is None or right is None:
        return left is right
    if tolerance is None:
        return left == right
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal(
            str(tolerance)
        )
    except (InvalidOperation, ValueError, TypeError):
        return left == right


def _key(row: dict, columns: list[str]) -> tuple:
    return tuple(_value(row, column) for column in columns)


def _row_equal(
    left: dict,
    right: dict,
    columns: list[str],
    tolerance: dict[str, float],
) -> tuple[bool, list[dict]]:
    differences = []
    for column in columns:
        left_value = _value(left, column)
        right_value = _value(right, column)
        if not _equal(left_value, right_value, tolerance.get(column)):
            differences.append(
                {
                    "column": column,
                    "actual": left_value,
                    "expected": right_value,
                    "tolerance": tolerance.get(column),
                }
            )
    return not differences, differences


def compare_rows(
    fact_id: str,
    spec: ComparatorSpec,
    actual: list[dict],
    expected: list[dict],
) -> ComparisonEvidence:
    if spec.type == "keyed_rows_equal":
        return _compare_keyed(fact_id, spec, actual, expected)
    if spec.type == "multiset_rows_equal":
        return _compare_multiset(fact_id, spec, actual, expected)
    return _compare_scalar(fact_id, spec, actual, expected)


def _compare_keyed(
    fact_id: str,
    spec: ComparatorSpec,
    actual: list[dict],
    expected: list[dict],
) -> ComparisonEvidence:
    actual_groups: dict[tuple, list[dict]] = {}
    expected_groups: dict[tuple, list[dict]] = {}
    for row in actual:
        actual_groups.setdefault(_key(row, spec.key_columns), []).append(row)
    for row in expected:
        expected_groups.setdefault(_key(row, spec.key_columns), []).append(row)

    duplicate_keys = []
    for side, groups in (("actual", actual_groups), ("expected", expected_groups)):
        for key, rows in groups.items():
            if len(rows) > 1:
                duplicate_keys.append(
                    {"side": side, "key": list(key), "count": len(rows)}
                )

    missing = [
        {"key": list(key), "row": rows[0]}
        for key, rows in expected_groups.items()
        if key not in actual_groups
    ]
    extra = [
        {"key": list(key), "row": rows[0]}
        for key, rows in actual_groups.items()
        if key not in expected_groups
    ]
    differences = []
    for key in sorted(
        set(actual_groups) & set(expected_groups),
        key=lambda item: repr(item),
    ):
        if len(actual_groups[key]) != 1 or len(expected_groups[key]) != 1:
            continue
        equal, row_differences = _row_equal(
            actual_groups[key][0],
            expected_groups[key][0],
            spec.compare_columns,
            spec.tolerance,
        )
        if not equal:
            differences.append({"key": list(key), "columns": row_differences})

    totals = {
        "missing": len(missing),
        "extra": len(extra),
        "duplicate_keys": len(duplicate_keys),
        "differences": len(differences),
    }
    matched = not any(totals.values())
    return ComparisonEvidence(
        fact_id=fact_id,
        comparator=spec.type,
        match=matched,
        actual_row_count=len(actual),
        expected_row_count=len(expected),
        missing=missing[:MAX_DIFFERENCE_ITEMS],
        extra=extra[:MAX_DIFFERENCE_ITEMS],
        duplicate_keys=duplicate_keys[:MAX_DIFFERENCE_ITEMS],
        differences=differences[:MAX_DIFFERENCE_ITEMS],
        difference_totals=totals,
        summary=(
            "结果一致"
            if matched
            else (
                f"缺少 {totals['missing']} 行，多出 {totals['extra']} 行，"
                f"值不同 {totals['differences']} 行，"
                f"重复键 {totals['duplicate_keys']} 个"
            )
        ),
    )


def _compare_multiset(
    fact_id: str,
    spec: ComparatorSpec,
    actual: list[dict],
    expected: list[dict],
) -> ComparisonEvidence:
    unmatched_expected = list(expected)
    extra = []
    for actual_row in actual:
        match_index = next(
            (
                index for index, expected_row in enumerate(unmatched_expected)
                if _row_equal(
                    actual_row,
                    expected_row,
                    spec.compare_columns,
                    spec.tolerance,
                )[0]
            ),
            None,
        )
        if match_index is None:
            extra.append({"row": actual_row})
        else:
            unmatched_expected.pop(match_index)
    missing = [{"row": row} for row in unmatched_expected]
    totals = {
        "missing": len(missing),
        "extra": len(extra),
        "duplicate_keys": 0,
        "differences": 0,
    }
    matched = not missing and not extra
    return ComparisonEvidence(
        fact_id=fact_id,
        comparator=spec.type,
        match=matched,
        actual_row_count=len(actual),
        expected_row_count=len(expected),
        missing=missing[:MAX_DIFFERENCE_ITEMS],
        extra=extra[:MAX_DIFFERENCE_ITEMS],
        difference_totals=totals,
        summary=(
            "结果一致"
            if matched else f"缺少 {len(missing)} 行，多出 {len(extra)} 行"
        ),
    )


def _compare_scalar(
    fact_id: str,
    spec: ComparatorSpec,
    actual: list[dict],
    expected: list[dict],
) -> ComparisonEvidence:
    differences = []
    if len(actual) == 1 and len(expected) == 1:
        _, differences = _row_equal(
            actual[0],
            expected[0],
            spec.compare_columns,
            spec.tolerance,
        )
    matched = len(actual) == 1 and len(expected) == 1 and not differences
    totals = {
        "missing": int(len(expected) == 1 and len(actual) == 0),
        "extra": int(len(actual) == 1 and len(expected) == 0),
        "duplicate_keys": 0,
        "differences": len(differences),
        "cardinality": int(len(actual) != 1 or len(expected) != 1),
    }
    return ComparisonEvidence(
        fact_id=fact_id,
        comparator=spec.type,
        match=matched,
        actual_row_count=len(actual),
        expected_row_count=len(expected),
        differences=differences[:MAX_DIFFERENCE_ITEMS],
        difference_totals=totals,
        summary=(
            "指标一致"
            if matched
            else f"实际 {len(actual)} 行，预期 {len(expected)} 行，差异 {len(differences)} 项"
        ),
    )
