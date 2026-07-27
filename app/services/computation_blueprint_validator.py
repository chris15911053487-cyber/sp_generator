"""Deterministic integrity checks for a frozen computation blueprint."""

from __future__ import annotations

from app.contracts.computation_blueprint import ComputationBlueprint
from app.contracts.semantic_design import FactBlueprint, ResultContract


class ComputationBlueprintError(ValueError):
    def __init__(self, code: str, message: str, *, evidence=None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _walk(expression):
    yield expression
    if expression.kind in {"binary", "unary", "function"}:
        for arg in expression.args:
            yield from _walk(arg)
    elif expression.kind == "case":
        for branch in expression.cases:
            yield from _walk(branch.when)
            yield from _walk(branch.then)
        if expression.else_expr is not None:
            yield from _walk(expression.else_expr)


_NUMERIC_TYPES = {"integer", "decimal", "money"}


def _compatible(left: str, right: str) -> bool:
    return (
        left == "unknown"
        or right == "unknown"
        or left == right
        or {left, right} <= _NUMERIC_TYPES
    )


def _merge_types(left: str, right: str) -> str:
    if left == "unknown":
        return right
    if right == "unknown":
        return left
    if left == right:
        return left
    if not _compatible(left, right):
        raise ComputationBlueprintError(
            "COMPUTATION_INPUT_TYPE_MISMATCH",
            "计算表达式两侧类型不兼容",
            evidence={"left": left, "right": right},
        )
    if "money" in {left, right}:
        return "money"
    if "decimal" in {left, right}:
        return "decimal"
    return "integer"


def _literal_type(value) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    return "string"


def _infer_type(expression, resolver) -> str:
    if expression.kind in {"input", "fact_value", "output", "parameter"}:
        return resolver(expression)
    if expression.kind == "literal":
        return _literal_type(expression.value)
    if expression.kind == "binary":
        left = _infer_type(expression.args[0], resolver)
        right = _infer_type(expression.args[1], resolver)
        if expression.operator in {"AND", "OR"}:
            if left != "boolean" or right != "boolean":
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "布尔运算只能使用 boolean 输入",
                    evidence={"left": left, "right": right},
                )
            return "boolean"
        if expression.operator in {"=", "<>", ">", ">=", "<", "<="}:
            if not _compatible(left, right):
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "比较运算两侧类型不兼容",
                    evidence={"left": left, "right": right},
                )
            return "boolean"
        if left not in _NUMERIC_TYPES or right not in _NUMERIC_TYPES:
            raise ComputationBlueprintError(
                "COMPUTATION_INPUT_TYPE_MISMATCH",
                "算术运算只能使用数值输入",
                evidence={"left": left, "right": right},
            )
        return _merge_types(left, right)
    if expression.kind == "unary":
        value_type = _infer_type(expression.args[0], resolver)
        if expression.operator == "NOT" and value_type != "boolean":
            raise ComputationBlueprintError(
                "COMPUTATION_INPUT_TYPE_MISMATCH",
                "NOT 只能使用 boolean 输入",
                evidence={"actual": value_type},
            )
        if expression.operator == "NEGATE" and value_type not in _NUMERIC_TYPES:
            raise ComputationBlueprintError(
                "COMPUTATION_INPUT_TYPE_MISMATCH",
                "NEGATE 只能使用数值输入",
                evidence={"actual": value_type},
            )
        if expression.operator in {"IS NULL", "IS NOT NULL", "NOT"}:
            return "boolean"
        return value_type
    if expression.kind == "function":
        arg_types = [_infer_type(arg, resolver) for arg in expression.args]
        if expression.operator == "ABS":
            if len(arg_types) != 1 or arg_types[0] not in _NUMERIC_TYPES:
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "ABS 需要一个数值输入",
                    evidence={"actual": arg_types},
                )
            return arg_types[0]
        if expression.operator in {"YEAR", "MONTH"}:
            if len(arg_types) != 1 or arg_types[0] not in {"date", "datetime"}:
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    f"{expression.operator} 需要日期输入",
                    evidence={"actual": arg_types},
                )
            return "integer"
        if expression.operator == "DATEFROMPARTS":
            if len(arg_types) != 3 or any(
                item != "integer" for item in arg_types
            ):
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "DATEFROMPARTS 需要三个 integer 输入",
                    evidence={"actual": arg_types},
                )
            return "date"
        if expression.operator == "EOMONTH":
            if len(arg_types) != 1 or arg_types[0] not in {"date", "datetime"}:
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "EOMONTH 需要日期输入",
                    evidence={"actual": arg_types},
                )
            return "date"
        if expression.operator == "CONCAT":
            return "string"
        if expression.operator in {"COALESCE", "NULLIF"}:
            inferred = "unknown"
            for item in arg_types:
                inferred = _merge_types(inferred, item)
            return inferred
    if expression.kind == "case":
        inferred = "unknown"
        for branch in expression.cases:
            when_type = _infer_type(branch.when, resolver)
            if when_type != "boolean":
                raise ComputationBlueprintError(
                    "COMPUTATION_INPUT_TYPE_MISMATCH",
                    "CASE 的 when 必须是 boolean",
                    evidence={"actual": when_type},
                )
            inferred = _merge_types(
                inferred,
                _infer_type(branch.then, resolver),
            )
        if expression.else_expr is not None:
            inferred = _merge_types(
                inferred,
                _infer_type(expression.else_expr, resolver),
            )
        return inferred
    raise ComputationBlueprintError(
        "COMPUTATION_INPUT_TYPE_MISMATCH",
        "无法推导计算表达式类型",
    )


def validate_computation_blueprint(
    result: ResultContract,
    facts: FactBlueprint,
    computations: ComputationBlueprint,
) -> None:
    if computations.result_contract_hash != result.content_hash:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_CHANGED",
            "计算蓝图引用的结果合同已经变化",
        )
    if computations.fact_blueprint_hash != facts.content_hash:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_CHANGED",
            "计算蓝图引用的事实蓝图已经变化",
        )
    expected_facts = {
        (fact.symbol, value.symbol): (
            value.logical_type,
            getattr(value, "aggregation", "none"),
        )
        for fact in facts.facts
        for value in fact.dimensions + fact.measures
    }
    actual_facts = {
        (item.fact_symbol, item.value_symbol): (
            item.logical_type,
            item.aggregation,
        )
        for item in computations.fact_values
    }
    missing = sorted(set(expected_facts) - set(actual_facts))
    extra = sorted(set(actual_facts) - set(expected_facts))
    changed = sorted(
        target
        for target in set(expected_facts) & set(actual_facts)
        if expected_facts[target] != actual_facts[target]
    )
    if missing:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_MISSING",
            "事实计算目标缺失",
            evidence={"missing": missing},
        )
    if extra or changed:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_CHANGED",
            "事实计算目标被增加或修改",
            evidence={"extra": extra, "changed": changed},
        )
    expected_outputs = {item.symbol for item in result.outputs}
    actual_outputs = {item.output_symbol for item in computations.results}
    if expected_outputs - actual_outputs:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_MISSING",
            "结果计算目标缺失",
            evidence={"missing": sorted(expected_outputs - actual_outputs)},
        )
    if actual_outputs - expected_outputs:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_CHANGED",
            "结果计算目标被增加",
            evidence={"extra": sorted(actual_outputs - expected_outputs)},
        )
    if result.result_mode == "exception_rows" and computations.result_filter is None:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_MISSING",
            "异常行结果必须定义结果过滤公式",
        )
    if result.result_mode != "exception_rows" and computations.result_filter is not None:
        raise ComputationBlueprintError(
            "COMPUTATION_TARGET_CHANGED",
            "当前结果模式不允许定义结果过滤公式",
        )

    fact_targets = set(expected_facts)
    fact_types = {
        target: definition[0]
        for target, definition in expected_facts.items()
    }
    output_symbols = expected_outputs
    output_types = {
        item.symbol: item.logical_type for item in result.outputs
    }
    parameter_symbols = {item.symbol for item in result.parameters}
    parameter_types = {
        item.symbol: item.logical_type for item in result.parameters
    }
    for computation in computations.fact_values:
        if computation.aggregation == "count_rows":
            if computation.logical_type != "integer":
                raise ComputationBlueprintError(
                    "COMPUTATION_RESULT_TYPE_MISMATCH",
                    "count_rows 的目标类型必须是 integer",
                )
            continue
        input_types = {
            item.symbol: item.logical_type for item in computation.inputs
        }
        actual_type = _infer_type(
            computation.expression,
            lambda node: input_types[node.symbol],
        )
        if not _compatible(actual_type, computation.logical_type):
            raise ComputationBlueprintError(
                "COMPUTATION_RESULT_TYPE_MISMATCH",
                "事实计算公式类型与冻结目标类型不一致",
                evidence={
                    "target": (
                        computation.fact_symbol,
                        computation.value_symbol,
                    ),
                    "expected": computation.logical_type,
                    "actual": actual_type,
                },
            )
    for item in computations.results:
        for node in _walk(item.expression):
            if node.kind == "fact_value":
                target = (node.fact_symbol, node.value_symbol)
                if target not in fact_targets:
                    raise ComputationBlueprintError(
                        "COMPUTATION_TARGET_CHANGED",
                        "结果公式引用了未知事实值",
                        evidence={"target": target},
                    )
            elif node.kind == "output" and node.symbol not in output_symbols:
                raise ComputationBlueprintError(
                    "COMPUTATION_TARGET_CHANGED",
                    "结果公式引用了未知输出",
                    evidence={"output": node.symbol},
                )
            elif node.kind == "parameter" and node.symbol not in parameter_symbols:
                raise ComputationBlueprintError(
                    "PARAMETER_CONTEXT_INVALID",
                    "结果公式引用了未知参数",
                    evidence={"parameter": node.symbol},
                )
        actual_type = _infer_type(
            item.expression,
            lambda node: (
                fact_types[(node.fact_symbol, node.value_symbol)]
                if node.kind == "fact_value"
                else output_types[node.symbol]
                if node.kind == "output"
                else parameter_types[node.symbol]
            ),
        )
        if not _compatible(actual_type, output_types[item.output_symbol]):
            raise ComputationBlueprintError(
                "COMPUTATION_RESULT_TYPE_MISMATCH",
                "结果计算公式类型与冻结输出类型不一致",
                evidence={
                    "output": item.output_symbol,
                    "expected": output_types[item.output_symbol],
                    "actual": actual_type,
                },
            )
    if computations.result_filter is not None:
        for node in _walk(computations.result_filter):
            if node.kind == "output" and node.symbol not in output_symbols:
                raise ComputationBlueprintError(
                    "COMPUTATION_TARGET_CHANGED",
                    "结果过滤引用了未知输出",
                    evidence={"output": node.symbol},
                )
            if node.kind == "parameter" and node.symbol not in parameter_symbols:
                raise ComputationBlueprintError(
                    "PARAMETER_CONTEXT_INVALID",
                    "结果过滤引用了未知参数",
                    evidence={"parameter": node.symbol},
                )
        filter_type = _infer_type(
            computations.result_filter,
            lambda node: (
                output_types[node.symbol]
                if node.kind == "output"
                else parameter_types[node.symbol]
            ),
        )
        if filter_type != "boolean":
            raise ComputationBlueprintError(
                "COMPUTATION_RESULT_TYPE_MISMATCH",
                "结果过滤公式必须返回 boolean",
                evidence={"actual": filter_type},
            )
