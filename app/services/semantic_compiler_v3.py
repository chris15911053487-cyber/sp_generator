"""Compile staged business contracts into the final SemanticContract."""

from __future__ import annotations

import hashlib

from app.contracts.computation_blueprint import ComputationBlueprint
from app.contracts.semantic import (
    SemanticContract,
    SemanticEntity,
    SemanticFact,
    SemanticFactDimension,
    SemanticFactJoin,
    SemanticFactJoinKey,
    SemanticFactMeasure,
    SemanticFactValueRef,
    SemanticFilter,
    SemanticOutput,
    SemanticParameter,
    SemanticResultBinding,
    SemanticResultExpression,
    SemanticResultWhenThen,
    SemanticSourceExpression,
    SemanticSourceField,
    SemanticSourceWhenThen,
)
from app.contracts.semantic_design import (
    ExpressionDesign,
    FactBlueprint,
    ResultContract,
    SourceRequirements,
    SymbolExpression,
)
from app.contracts.semantic_obligations import SemanticObligationSet
from app.contracts.semantic_input_obligations import SemanticInputObligationSet
from app.services.expression_materializer import (
    ExpressionMaterializationError,
    materialize_expression_design,
)
from app.services.semantic_symbols import SemanticSymbolTable


class SemanticCompileError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _source_expression(
    expression: SymbolExpression,
    symbols: SemanticSymbolTable,
) -> SemanticSourceExpression:
    if expression.kind == "source":
        return SemanticSourceExpression(
            kind="field",
            field_id=symbols.resolve("source", expression.symbol),
        )
    if expression.kind == "literal":
        return SemanticSourceExpression(kind="literal", value=expression.value)
    if expression.kind in {"binary", "unary", "function"}:
        return SemanticSourceExpression(
            kind=expression.kind,
            operator=str(expression.operator).upper(),
            args=[
                _source_expression(item, symbols)
                for item in expression.args
            ],
        )
    if expression.kind == "case":
        return SemanticSourceExpression(
            kind="case",
            cases=[
                SemanticSourceWhenThen(
                    when=_source_expression(item.when, symbols),
                    then=_source_expression(item.then, symbols),
                )
                for item in expression.cases
            ],
            else_expr=(
                _source_expression(expression.else_expr, symbols)
                if expression.else_expr is not None else None
            ),
        )
    raise SemanticCompileError(
        "EXPRESSION_KIND_INVALID",
        f"事实表达式不允许 {expression.kind}",
    )


def _result_expression(
    expression: SymbolExpression,
    symbols: SemanticSymbolTable,
) -> SemanticResultExpression:
    if expression.kind == "fact_value":
        fact_id, value_id = symbols.resolve_fact_value(
            expression.fact_symbol, expression.value_symbol,
        )
        return SemanticResultExpression(
            kind="fact_value",
            fact_value=SemanticFactValueRef(
                fact_id=fact_id,
                value_id=value_id,
            ),
        )
    if expression.kind in {"output", "parameter"}:
        field = "output_id" if expression.kind == "output" else "parameter_id"
        namespace = "output" if expression.kind == "output" else "parameter"
        return SemanticResultExpression(
            kind=expression.kind,
            **{field: symbols.resolve(namespace, expression.symbol)},
        )
    if expression.kind == "literal":
        return SemanticResultExpression(kind="literal", value=expression.value)
    if expression.kind in {"binary", "unary", "function"}:
        return SemanticResultExpression(
            kind=expression.kind,
            operator=str(expression.operator).upper(),
            args=[
                _result_expression(item, symbols)
                for item in expression.args
            ],
        )
    if expression.kind == "case":
        return SemanticResultExpression(
            kind="case",
            cases=[
                SemanticResultWhenThen(
                    when=_result_expression(item.when, symbols),
                    then=_result_expression(item.then, symbols),
                )
                for item in expression.cases
            ],
            else_expr=(
                _result_expression(expression.else_expr, symbols)
                if expression.else_expr is not None else None
            ),
        )
    raise SemanticCompileError(
        "EXPRESSION_KIND_INVALID",
        f"结果表达式不允许 {expression.kind}",
    )


def _walk_symbol_expression(expression: SymbolExpression):
    yield expression
    for child in getattr(expression, "args", ()):
        yield from _walk_symbol_expression(child)
    for case in getattr(expression, "cases", ()):
        yield from _walk_symbol_expression(case.when)
        yield from _walk_symbol_expression(case.then)
    else_expr = getattr(expression, "else_expr", None)
    if else_expr is not None:
        yield from _walk_symbol_expression(else_expr)


def _require_function_arity(
    operator: str,
    actual: int,
    *allowed: int,
) -> None:
    if actual in allowed:
        return
    expected = " 或 ".join(str(item) for item in allowed)
    raise SemanticCompileError(
        "EXPRESSION_FUNCTION_ARITY_INVALID",
        f"{operator} 需要 {expected} 个参数，实际为 {actual} 个",
        evidence={
            "operator": operator,
            "expected": list(allowed),
            "actual": actual,
        },
    )


def _require_argument_types(
    operator: str,
    actual: list[str | None],
    allowed: set[str],
) -> None:
    invalid = [
        {"index": index, "actual": logical_type}
        for index, logical_type in enumerate(actual)
        if logical_type is not None and logical_type not in allowed
    ]
    if not invalid:
        return
    raise SemanticCompileError(
        "EXPRESSION_FUNCTION_ARGUMENT_TYPE_INVALID",
        f"{operator} 的参数类型不符合定义",
        evidence={
            "operator": operator,
            "allowed": sorted(allowed),
            "invalid": invalid,
        },
    )


def _infer_source_expression_type(
    expression: SymbolExpression,
    source_types: dict[str, str],
) -> str | None:
    if expression.kind == "source":
        return source_types.get(str(expression.symbol).casefold())
    if expression.kind == "literal":
        if expression.value is None:
            return None
        if isinstance(expression.value, bool):
            return "boolean"
        if isinstance(expression.value, int):
            return "integer"
        if isinstance(expression.value, float):
            return "decimal"
        return "string"
    if expression.kind == "binary":
        operator = str(expression.operator).upper()
        values = [
            _infer_source_expression_type(item, source_types)
            for item in expression.args
        ]
        if operator in {"=", "<>", ">", ">=", "<", "<=", "AND", "OR"}:
            return "boolean"
        if "money" in values:
            return "money"
        if "decimal" in values:
            return "decimal"
        return values[0] if values else None
    if expression.kind == "unary":
        if str(expression.operator).upper() == "NEGATE":
            return _infer_source_expression_type(
                expression.args[0], source_types,
            )
        return "boolean"
    if expression.kind == "function":
        operator = str(expression.operator).upper()
        values = [
            _infer_source_expression_type(item, source_types)
            for item in expression.args
        ]
        if operator in {"YEAR", "MONTH"}:
            _require_function_arity(operator, len(values), 1)
            _require_argument_types(operator, values, {"date", "datetime"})
            return "integer"
        if operator == "DATEFROMPARTS":
            _require_function_arity(operator, len(values), 3)
            _require_argument_types(operator, values, {"integer"})
            return "date"
        if operator == "EOMONTH":
            _require_function_arity(operator, len(values), 1)
            _require_argument_types(operator, values, {"date", "datetime"})
            return "date"
        if operator == "CONCAT":
            return "string"
        if operator == "ABS":
            _require_function_arity(operator, len(values), 1)
            _require_argument_types(
                operator, values, {"integer", "decimal", "money"},
            )
            return values[0]
        if operator == "NULLIF":
            _require_function_arity(operator, len(values), 2)
            return values[0]
        if operator == "COALESCE":
            return next((item for item in values if item is not None), None)
        raise SemanticCompileError(
            "EXPRESSION_FUNCTION_UNSUPPORTED",
            f"源表达式不支持函数 {operator}",
            evidence={"operator": operator},
        )
    if expression.kind == "case":
        values = [
            _infer_source_expression_type(item.then, source_types)
            for item in expression.cases
        ]
        if expression.else_expr is not None:
            values.append(_infer_source_expression_type(
                expression.else_expr, source_types,
            ))
        return next((item for item in values if item is not None), None)
    return None


def _assert_compatible_type(
    *,
    expected: str,
    actual: str | None,
    element: str,
) -> None:
    if actual is None:
        return
    if actual == expected or {actual, expected} == {"money", "decimal"}:
        return
    raise SemanticCompileError(
        "EXPRESSION_TYPE_MISMATCH",
        f"{element} 的表达式类型 {actual} 与声明类型 {expected} 不一致",
        evidence={
            "element": element,
            "expected": expected,
            "actual": actual,
        },
    )


def compile_semantic_contract(
    result: ResultContract,
    blueprint: FactBlueprint,
    sources: SourceRequirements,
    expressions: ExpressionDesign,
    obligations: SemanticObligationSet,
    *,
    computations: ComputationBlueprint,
    input_obligations: SemanticInputObligationSet,
    decision_hash: str,
    confirmed_decision_keys: set[str] | None = None,
) -> tuple[SemanticContract, dict]:
    if obligations.result_contract_hash != result.content_hash:
        raise SemanticCompileError(
            "OBLIGATION_TARGET_CHANGED",
            "业务政策义务对应的结果契约已经变化，必须重新编译义务。",
            evidence={
                "expected": obligations.result_contract_hash,
                "actual": result.content_hash,
                "target": "result_contract",
            },
        )
    if obligations.fact_blueprint_hash != blueprint.content_hash:
        raise SemanticCompileError(
            "OBLIGATION_TARGET_CHANGED",
            "业务政策义务对应的事实蓝图已经变化，必须重新编译义务。",
            evidence={
                "expected": obligations.fact_blueprint_hash,
                "actual": blueprint.content_hash,
                "target": "fact_blueprint",
            },
        )
    if computations.result_contract_hash != result.content_hash:
        raise SemanticCompileError(
            "COMPUTATION_TARGET_CHANGED",
            "计算蓝图对应的结果合同已经变化。",
        )
    if computations.fact_blueprint_hash != blueprint.content_hash:
        raise SemanticCompileError(
            "COMPUTATION_TARGET_CHANGED",
            "计算蓝图对应的事实蓝图已经变化。",
        )
    expected_input_hashes = (
        result.content_hash,
        blueprint.content_hash,
        computations.content_hash,
    )
    actual_input_hashes = (
        input_obligations.result_contract_hash,
        input_obligations.fact_blueprint_hash,
        input_obligations.computation_blueprint_hash,
    )
    if actual_input_hashes != expected_input_hashes:
        raise SemanticCompileError(
            "COMPUTATION_TARGET_CHANGED",
            "来源输入义务对应的上游合同已经变化。",
            evidence={
                "expected": expected_input_hashes,
                "actual": actual_input_hashes,
            },
        )
    required_source_inputs = {
        item.slot_name for item in input_obligations.inputs
    }
    actual_source_symbols = {item.symbol for item in sources.fields}
    missing_source_inputs = sorted(
        required_source_inputs - actual_source_symbols
    )
    if missing_source_inputs:
        raise SemanticCompileError(
            "SOURCE_INPUT_IMPLEMENTATION_MISSING",
            "冻结计算输入没有完整的来源实现。",
            evidence={"missing": missing_source_inputs},
        )
    symbols = SemanticSymbolTable()
    for item in result.parameters:
        symbols.register("parameter", item.symbol)
    for item in result.outputs:
        symbols.register("output", item.symbol)
    for item in sources.entities:
        symbols.register("entity", item.symbol)
    for item in sources.fields:
        symbols.register("source", item.symbol)
    for item in sources.filters:
        symbols.register("filter", item.symbol)
    for fact in blueprint.facts:
        symbols.register("fact", fact.symbol)
    for fact in blueprint.facts:
        for value in fact.dimensions + fact.measures:
            symbols.register_fact_value(fact.symbol, value.symbol)

    known_facts = {item.symbol.casefold(): item for item in blueprint.facts}
    known_entities = {item.symbol.casefold() for item in sources.entities}
    for fact in blueprint.facts:
        unknown = sorted(
            set(item.casefold() for item in fact.entity_symbols)
            - known_entities
        )
        if unknown:
            raise SemanticCompileError(
                "SOURCE_ENTITY_UNKNOWN",
                f"事实 {fact.symbol} 引用未知实体",
                evidence={"unknown": unknown},
            )

    source_by_symbol = {
        item.symbol.casefold(): item for item in sources.fields
    }
    fact_entities = {
        item.symbol.casefold(): {
            value.casefold() for value in item.entity_symbols
        }
        for item in blueprint.facts
    }
    for obligation in input_obligations.inputs:
        source = source_by_symbol.get(obligation.slot_name.casefold())
        allowed_entities = fact_entities.get(
            obligation.fact_symbol.casefold(),
            set(),
        )
        if (
            source is None
            or source.entity_symbol.casefold() not in allowed_entities
        ):
            raise SemanticCompileError(
                "SOURCE_INPUT_OWNER_UNKNOWN",
                "来源输入实现不属于冻结的目标事实实体。",
                evidence={
                    "slot": obligation.slot_name,
                    "fact": obligation.fact_symbol,
                    "actual_entity": (
                        source.entity_symbol if source is not None else None
                    ),
                    "allowed_entities": sorted(allowed_entities),
                },
            )
    source_types = {
        key: item.logical_type for key, item in source_by_symbol.items()
    }
    dimension_designs = {
        (item.fact_symbol.casefold(), item.dimension_symbol.casefold()): item
        for item in expressions.dimensions
    }
    measure_designs = {
        (item.fact_symbol.casefold(), item.measure_symbol.casefold()): item
        for item in expressions.measures
    }
    if len(dimension_designs) != len(expressions.dimensions):
        raise SemanticCompileError(
            "FACT_DIMENSION_DUPLICATE", "事实维度实现重复",
        )
    if len(measure_designs) != len(expressions.measures):
        raise SemanticCompileError(
            "FACT_MEASURE_DUPLICATE", "事实指标实现重复",
        )

    expected_dimensions = {
        (fact.symbol.casefold(), item.symbol.casefold())
        for fact in blueprint.facts
        for item in fact.dimensions
    }
    expected_measures = {
        (fact.symbol.casefold(), item.symbol.casefold())
        for fact in blueprint.facts
        for item in fact.measures
    }
    extra_dimensions = sorted(set(dimension_designs) - expected_dimensions)
    extra_measures = sorted(set(measure_designs) - expected_measures)
    if extra_dimensions or extra_measures:
        raise SemanticCompileError(
            "EXPRESSION_TARGET_UNKNOWN",
            "表达式设计实现了上游事实蓝图中不存在的目标",
            evidence={
                "dimensions": extra_dimensions,
                "measures": extra_measures,
            },
        )
    if result.result_mode == "exception_rows":
        if expressions.result_filter is None:
            raise SemanticCompileError(
                "RESULT_FILTER_MISSING",
                "exception_rows 结果必须声明异常选择公式",
            )
    elif expressions.result_filter is not None:
        raise SemanticCompileError(
            "RESULT_FILTER_UNEXPECTED",
            "非 exception_rows 结果不得添加异常选择公式",
        )

    consumed_source_symbols: set[str] = {
        item.source_symbol.casefold() for item in sources.filters
    }
    expression_roots = [
        item.expression for item in expressions.dimensions
    ] + [
        item.expression for item in expressions.measures
        if item.expression is not None
    ]
    for expression in expression_roots:
        consumed_source_symbols.update(
            str(item.symbol).casefold()
            for item in _walk_symbol_expression(expression)
            if item.kind == "source" and item.symbol
        )
    unused_sources = sorted(
        set(source_by_symbol) - consumed_source_symbols
    )
    if unused_sources:
        raise SemanticCompileError(
            "SOURCE_FIELD_UNUSED",
            "底层业务字段没有被事实、过滤或表达式消费",
            evidence={"unused": unused_sources},
        )
    known_fact_symbols = set(known_facts)
    for semantic_filter in sources.filters:
        unknown_filter_facts = sorted(
            set(item.casefold() for item in semantic_filter.fact_symbols)
            - known_fact_symbols
        )
        if unknown_filter_facts:
            raise SemanticCompileError(
                "SOURCE_FILTER_FACT_UNKNOWN",
                f"过滤 {semantic_filter.symbol} 引用了未知事实",
                evidence={"unknown": unknown_filter_facts},
            )
        source = source_by_symbol[semantic_filter.source_symbol.casefold()]
        invalid_owners = sorted(
            fact_symbol
            for fact_symbol in semantic_filter.fact_symbols
            if source.entity_symbol.casefold() not in fact_entities.get(
                fact_symbol.casefold(),
                set(),
            )
        )
        if invalid_owners:
            raise SemanticCompileError(
                "SOURCE_INPUT_OWNER_UNKNOWN",
                "过滤来源字段不属于过滤所针对的事实实体。",
                evidence={
                    "filter": semantic_filter.symbol,
                    "source": semantic_filter.source_symbol,
                    "actual_entity": source.entity_symbol,
                    "facts": invalid_owners,
                },
            )
    for fact in blueprint.facts:
        expected_policies = {
            item.policy_key.casefold()
            for item in obligations.obligations
            if (
                item.kind == "fact_filter"
                and item.fact_symbol.casefold() == fact.symbol.casefold()
            )
        }
        actual_policies = {
            str(item.policy_key).casefold()
            for item in sources.filters
            if (
                item.policy_key
                and fact.symbol.casefold()
                in {value.casefold() for value in item.fact_symbols}
            )
        }
        if expected_policies != actual_policies:
            raise SemanticCompileError(
                "FACT_FILTER_POLICY_MISMATCH",
                f"事实 {fact.symbol} 的过滤政策没有按蓝图完整实现",
                evidence={
                    "expected": sorted(expected_policies),
                    "actual": sorted(actual_policies),
                },
            )
    dimension_types = {
        (item.fact_symbol.casefold(), item.dimension_symbol.casefold()):
        _infer_source_expression_type(item.expression, source_types)
        for item in expressions.dimensions
    }
    for join in blueprint.joins:
        left_type = dimension_types.get((
            join.left_fact_symbol.casefold(),
            join.left_dimension_symbol.casefold(),
        ))
        right_type = dimension_types.get((
            join.right_fact_symbol.casefold(),
            join.right_dimension_symbol.casefold(),
        ))
        if (
            left_type is not None
            and right_type is not None
            and left_type != right_type
            and {left_type, right_type} != {"money", "decimal"}
        ):
            raise SemanticCompileError(
                "FACT_JOIN_TYPE_MISMATCH",
                f"事实关联 {join.symbol} 的两侧维度类型不一致",
                evidence={"left": left_type, "right": right_type},
            )

    semantic_facts = []
    for fact in blueprint.facts:
        dimensions = []
        for dimension in fact.dimensions:
            design = dimension_designs.get((
                fact.symbol.casefold(), dimension.symbol.casefold(),
            ))
            if design is None:
                raise SemanticCompileError(
                    "FACT_DIMENSION_MISSING",
                    f"事实维度 {fact.symbol}.{dimension.symbol} 未实现",
                )
            if design.logical_type != dimension.logical_type:
                raise SemanticCompileError(
                    "FACT_DIMENSION_BLUEPRINT_MISMATCH",
                    f"事实维度 {fact.symbol}.{dimension.symbol} 改变了已冻结类型",
                    evidence={
                        "expected": dimension.logical_type,
                        "actual": design.logical_type,
                    },
                )
            _assert_compatible_type(
                expected=design.logical_type,
                actual=_infer_source_expression_type(
                    design.expression, source_types,
                ),
                element=f"{fact.symbol}.{dimension.symbol}",
            )
            if design.expression.kind == "source":
                dimensions.append(SemanticFactDimension(
                    id=symbols.resolve_fact_value(
                        fact.symbol, dimension.symbol,
                    )[1],
                    field_id=symbols.resolve(
                        "source", design.expression.symbol,
                    ),
                    meaning=dimension.meaning,
                ))
            else:
                dimensions.append(SemanticFactDimension(
                    id=symbols.resolve_fact_value(
                        fact.symbol, dimension.symbol,
                    )[1],
                    expression=_source_expression(
                        design.expression, symbols,
                    ),
                    meaning=dimension.meaning,
                    logical_type=design.logical_type,
                ))
        measures = []
        for measure in fact.measures:
            design = measure_designs.get((
                fact.symbol.casefold(), measure.symbol.casefold(),
            ))
            if design is None:
                raise SemanticCompileError(
                    "FACT_MEASURE_MISSING",
                    f"事实指标 {fact.symbol}.{measure.symbol} 未实现",
                )
            if (
                design.aggregation != measure.aggregation
                or design.logical_type != measure.logical_type
            ):
                raise SemanticCompileError(
                    "FACT_MEASURE_BLUEPRINT_MISMATCH",
                    f"事实指标 {fact.symbol}.{measure.symbol} 改变了已冻结蓝图",
                    evidence={
                        "expected_aggregation": measure.aggregation,
                        "actual_aggregation": design.aggregation,
                        "expected_type": measure.logical_type,
                        "actual_type": design.logical_type,
                    },
                )
            kwargs = {}
            if design.aggregation != "count_rows":
                if design.expression is None:
                    raise SemanticCompileError(
                        "FACT_MEASURE_SOURCE_MISSING",
                        f"事实指标 {fact.symbol}.{measure.symbol} 缺少实现",
                    )
                if design.expression.kind == "source":
                    kwargs["field_id"] = symbols.resolve(
                        "source", design.expression.symbol,
                    )
                else:
                    kwargs["expression"] = _source_expression(
                        design.expression, symbols,
                    )
                _assert_compatible_type(
                    expected=design.logical_type,
                    actual=_infer_source_expression_type(
                        design.expression, source_types,
                    ),
                    element=f"{fact.symbol}.{measure.symbol}",
                )
            measures.append(SemanticFactMeasure(
                id=symbols.resolve_fact_value(
                    fact.symbol, measure.symbol,
                )[1],
                meaning=measure.meaning,
                aggregation=design.aggregation,
                logical_type=design.logical_type,
                **kwargs,
            ))
        semantic_facts.append(SemanticFact(
            id=symbols.resolve("fact", fact.symbol),
            meaning=fact.meaning,
            entity_ids=[
                symbols.resolve("entity", item)
                for item in fact.entity_symbols
            ],
            dimensions=dimensions,
            measures=measures,
            filter_ids=[
                symbols.resolve("filter", item.symbol)
                for item in sources.filters
                if fact.symbol.casefold() in {
                    value.casefold() for value in item.fact_symbols
                }
            ],
            grain=[
                symbols.resolve_fact_value(fact.symbol, item)[1]
                for item in fact.grain_dimension_symbols
            ],
        ))

    raw_result_bindings = {
        item.output_symbol.casefold(): item.expression
        for item in expressions.results
    }
    if len(raw_result_bindings) != len(expressions.results):
        raise SemanticCompileError(
            "RESULT_BINDING_DUPLICATE",
            "同一结果输出被重复绑定",
        )
    output_symbols = {
        item.symbol.casefold(): item for item in result.outputs
    }
    extra_result_targets = sorted(
        set(raw_result_bindings) - set(output_symbols)
    )
    if extra_result_targets:
        raise SemanticCompileError(
            "EXPRESSION_TARGET_UNKNOWN",
            "结果表达式绑定了不存在的输出",
            evidence={"outputs": extra_result_targets},
        )
    parameter_types = {
        item.symbol.casefold(): item.logical_type
        for item in result.parameters
    }
    fact_value_types = {
        **{
            (item.fact_symbol.casefold(), item.dimension_symbol.casefold()):
            item.logical_type
            for item in expressions.dimensions
        },
        **{
            (item.fact_symbol.casefold(), item.measure_symbol.casefold()):
            item.logical_type
            for item in expressions.measures
        },
    }

    def output_dependencies(expression: SymbolExpression) -> set[str]:
        return {
            str(item.symbol).casefold()
            for item in _walk_symbol_expression(expression)
            if item.kind == "output" and item.symbol
        }

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit_output(symbol: str):
        if symbol in visited:
            return
        if symbol in visiting:
            raise SemanticCompileError(
                "RESULT_DEPENDENCY_CYCLE",
                f"最终输出公式存在循环依赖：{symbol}",
                evidence={"output": symbol},
            )
        expression = raw_result_bindings.get(symbol)
        if expression is None:
            return
        visiting.add(symbol)
        for dependency in output_dependencies(expression):
            if dependency not in output_symbols:
                raise SemanticCompileError(
                    "EXPRESSION_SYMBOL_UNKNOWN",
                    f"最终公式引用了未知输出：{dependency}",
                )
            visit_output(dependency)
        visiting.remove(symbol)
        visited.add(symbol)

    for output_symbol in raw_result_bindings:
        visit_output(output_symbol)

    def infer_result_type(
        expression: SymbolExpression,
        resolving: tuple[str, ...] = (),
    ) -> str | None:
        if expression.kind == "fact_value":
            return fact_value_types.get((
                str(expression.fact_symbol).casefold(),
                str(expression.value_symbol).casefold(),
            ))
        if expression.kind == "parameter":
            return parameter_types.get(str(expression.symbol).casefold())
        if expression.kind == "output":
            symbol = str(expression.symbol).casefold()
            if symbol in resolving:
                return output_symbols.get(symbol).logical_type
            nested = raw_result_bindings.get(symbol)
            return (
                infer_result_type(nested, resolving + (symbol,))
                if nested is not None
                else output_symbols.get(symbol).logical_type
                if symbol in output_symbols
                else None
            )
        if expression.kind == "literal":
            if expression.value is None:
                return None
            if isinstance(expression.value, bool):
                return "boolean"
            if isinstance(expression.value, int):
                return "integer"
            if isinstance(expression.value, float):
                return "decimal"
            return "string"
        if expression.kind == "binary":
            operator = str(expression.operator).upper()
            values = [
                infer_result_type(item, resolving)
                for item in expression.args
            ]
            if operator in {"=", "<>", ">", ">=", "<", "<=", "AND", "OR"}:
                return "boolean"
            if "money" in values:
                return "money"
            if "decimal" in values:
                return "decimal"
            return values[0] if values else None
        if expression.kind == "unary":
            if str(expression.operator).upper() == "NEGATE":
                return infer_result_type(expression.args[0], resolving)
            return "boolean"
        if expression.kind == "function":
            operator = str(expression.operator).upper()
            values = [
                infer_result_type(item, resolving)
                for item in expression.args
            ]
            if operator == "ABS":
                _require_function_arity(operator, len(values), 1)
                _require_argument_types(
                    operator, values, {"integer", "decimal", "money"},
                )
                return values[0]
            if operator == "NULLIF":
                _require_function_arity(operator, len(values), 2)
                return values[0]
            if operator == "COALESCE":
                return next((item for item in values if item is not None), None)
            raise SemanticCompileError(
                "EXPRESSION_FUNCTION_UNSUPPORTED",
                f"结果表达式不支持函数 {operator}",
                evidence={"operator": operator},
            )
        if expression.kind == "case":
            values = [
                infer_result_type(item.then, resolving)
                for item in expression.cases
            ]
            if expression.else_expr is not None:
                values.append(infer_result_type(
                    expression.else_expr, resolving,
                ))
            return next((item for item in values if item is not None), None)
        return None

    for output_symbol, expression in raw_result_bindings.items():
        expected_type = output_symbols[output_symbol].logical_type
        actual_type = infer_result_type(expression)
        if (
            actual_type is not None
            and actual_type != expected_type
            and {actual_type, expected_type} != {"money", "decimal"}
        ):
            raise SemanticCompileError(
                "SEMANTIC_RESULT_TYPE_MISMATCH",
                f"最终输出 {output_symbol} 的公式类型不正确",
                evidence={
                    "output": output_symbol,
                    "expected": expected_type,
                    "actual": actual_type,
                },
            )
    if expressions.result_filter is not None:
        filter_type = infer_result_type(expressions.result_filter)
        if filter_type != "boolean":
            raise SemanticCompileError(
                "RESULT_FILTER_TYPE_MISMATCH",
                "异常选择公式必须返回 boolean",
                evidence={"actual": filter_type},
            )

    result_bindings = {}
    for item in expressions.results:
        output_id = symbols.resolve("output", item.output_symbol)
        if output_id in result_bindings:
            raise SemanticCompileError(
                "RESULT_BINDING_DUPLICATE",
                f"输出 {item.output_symbol} 重复绑定",
            )
        result_bindings[output_id] = SemanticResultBinding(
            output_id=output_id,
            expression=_result_expression(item.expression, symbols),
        )
    expected_outputs = {
        symbols.resolve("output", item.symbol) for item in result.outputs
    }
    missing_outputs = sorted(expected_outputs - set(result_bindings))
    if missing_outputs:
        raise SemanticCompileError(
            "RESULT_BINDING_MISSING",
            "结果输出未全部绑定",
            evidence={"missing": missing_outputs},
        )

    policies_by_key = {
        item.key.casefold(): item for item in result.business_policies
    }
    consumed = set(policies_by_key)
    missing_decisions = sorted(
        {
            item.casefold() for item in (confirmed_decision_keys or set())
        } - consumed
    )
    if missing_decisions:
        raise SemanticCompileError(
            "DECISION_NOT_CONSUMED",
            "已确认业务决策没有进入语义合同",
            evidence={"missing": missing_decisions},
        )
    unexpected_policies = sorted(
        consumed - {
            item.casefold() for item in (confirmed_decision_keys or set())
        }
    ) if confirmed_decision_keys is not None else []
    if unexpected_policies:
        raise SemanticCompileError(
            "POLICY_UNKNOWN",
            "结果契约包含未经确认的业务政策。",
            evidence={"unexpected": unexpected_policies},
        )

    policy_filters: dict[tuple[str, str], list[str]] = {}
    invalid_policy_filters = []
    for item in sources.filters:
        if not item.policy_key:
            continue
        if len(item.fact_symbols) != 1:
            invalid_policy_filters.append(item.symbol)
            continue
        target = (
            item.policy_key.casefold(),
            item.fact_symbols[0].casefold(),
        )
        policy_filters.setdefault(target, []).append(item.symbol)

    try:
        expected_expressions = materialize_expression_design(
            blueprint,
            computations,
            input_obligations,
            sources,
        )
    except ExpressionMaterializationError as exc:
        raise SemanticCompileError(
            exc.code,
            str(exc),
            evidence=exc.evidence,
        ) from exc
    if expected_expressions.content_hash != expressions.content_hash:
        raise SemanticCompileError(
            "POLICY_COMPUTATION_NOT_COVERED",
            "内部表达式与冻结计算蓝图不等价。",
            evidence={
                "computation_hash": computations.content_hash,
                "expected_expression_hash": expected_expressions.content_hash,
                "actual_expression_hash": expressions.content_hash,
            },
        )

    coverage = []
    computations_by_target = {
        (item.fact_symbol.casefold(), item.value_symbol.casefold()): item
        for item in computations.fact_values
    }
    input_obligations_by_fact = {}
    for item in input_obligations.inputs:
        input_obligations_by_fact.setdefault(
            item.fact_symbol.casefold(),
            {},
        )[item.input_symbol.casefold()] = item
    expected_filter_targets = set()
    for obligation in obligations.obligations:
        policy = policies_by_key[obligation.policy_key.casefold()]
        target = {
            "fact_symbol": obligation.fact_symbol,
            "value_symbol": obligation.value_symbol,
            "join_symbol": obligation.join_symbol,
            "match_mode": obligation.match_mode,
        }
        target = {
            key: value for key, value in target.items()
            if value is not None
        }
        implementation = None
        if obligation.kind == "fact_filter":
            filter_target = (
                obligation.policy_key.casefold(),
                str(obligation.fact_symbol).casefold(),
            )
            expected_filter_targets.add(filter_target)
            matches = policy_filters.get(filter_target, [])
            if len(matches) == 1:
                implementation = {
                    "stage": "source_requirements",
                    "symbol": matches[0],
                }
        elif obligation.kind == "fact_expression":
            expression_target = (
                str(obligation.fact_symbol).casefold(),
                str(obligation.value_symbol).casefold(),
            )
            if (
                expression_target in dimension_designs
                or expression_target in measure_designs
            ):
                implementation = {
                    "stage": "expression_materialize",
                    "symbol": ".".join(expression_target),
                }
                computation = computations_by_target.get(expression_target)
                required_inputs = (
                    [
                        item.symbol
                        for item in computation.inputs
                    ]
                    if computation is not None else []
                )
                implemented_inputs = [
                    input_obligations_by_fact.get(
                        expression_target[0],
                        {},
                    )[item.casefold()].slot_name
                    for item in required_inputs
                    if item.casefold() in input_obligations_by_fact.get(
                        expression_target[0],
                        {},
                    )
                ]
                if (
                    computation is None
                    or len(implemented_inputs) != len(required_inputs)
                ):
                    implementation = None
        elif obligation.kind == "join":
            join = next(
                (
                    item for item in blueprint.joins
                    if item.symbol.casefold()
                    == str(obligation.join_symbol).casefold()
                ),
                None,
            )
            expected_join_type = {
                "matched_only": "inner",
                "left_preserved": "left",
                "include_unmatched": "full",
            }[obligation.match_mode]
            if join is not None and join.join_type == expected_join_type:
                implementation = {
                    "stage": "fact_blueprint",
                    "symbol": join.symbol,
                }
        elif obligation.kind == "result_filter":
            if expressions.result_filter is not None:
                implementation = {
                    "stage": "expression_materialize",
                    "symbol": "result_filter",
                }
        elif obligation.kind == "contract_only":
            implementation = {
                "stage": "result_contract",
                "symbol": obligation.policy_key,
            }
        if implementation is None:
            raise SemanticCompileError(
                "OBLIGATION_IMPLEMENTATION_MISSING",
                f"业务政策 {obligation.policy_key} 没有在指定语义位置实现。",
                evidence={
                    "obligation_id": obligation.obligation_id,
                    "policy_key": obligation.policy_key,
                    "policy_value": policy.value,
                    "effect": policy.effect,
                    "kind": obligation.kind,
                    "target": target,
                },
            )
        coverage_item = {
            "obligation_id": obligation.obligation_id,
            "policy_key": obligation.policy_key,
            "policy_value": policy.value,
            "effect": policy.effect,
            "kind": obligation.kind,
            "target": target,
            "implementation": implementation,
            "implemented_by": (
                implementation["stage"] + ":" + implementation["symbol"]
            ),
            "status": "covered",
        }
        if obligation.kind == "fact_expression":
            computation = computations_by_target[(
                str(obligation.fact_symbol).casefold(),
                str(obligation.value_symbol).casefold(),
            )]
            required_inputs = [item.symbol for item in computation.inputs]
            coverage_item.update({
                "computation_hash": computation.content_hash,
                "formula": (
                    computation.expression.model_dump(mode="json")
                    if computation.expression is not None else None
                ),
                "required_inputs": required_inputs,
                "implemented_inputs": implemented_inputs,
            })
        coverage.append(coverage_item)

    extra_filter_targets = sorted(
        set(policy_filters) - expected_filter_targets
    )
    duplicate_filter_targets = sorted(
        target for target, target_filters in policy_filters.items()
        if len(target_filters) != 1
    )
    if (
        invalid_policy_filters
        or extra_filter_targets
        or duplicate_filter_targets
    ):
        raise SemanticCompileError(
            "OBLIGATION_IMPLEMENTATION_EXTRA",
            "来源需求包含未被业务政策义务授权或重复的政策过滤。",
            evidence={
                "invalid_filters": sorted(invalid_policy_filters),
                "extra_targets": extra_filter_targets,
                "duplicate_targets": duplicate_filter_targets,
            },
        )

    contract_hash_seed = hashlib.sha256(
        (
            result.content_hash + blueprint.content_hash
            + computations.content_hash + obligations.content_hash
            + input_obligations.content_hash
            + sources.content_hash + expressions.content_hash
        ).encode("ascii")
    ).hexdigest()
    contract = SemanticContract(
        contract_id=f"{decision_hash}:{contract_hash_seed[:24]}",
        procedure_name=result.procedure_name,
        purpose=result.purpose,
        result_mode=result.result_mode,
        parameters=[
            SemanticParameter(
                id=symbols.resolve("parameter", item.symbol),
                name=item.name,
                logical_type=item.logical_type,
                required=item.required,
                default=item.default,
                meaning=item.meaning,
                boundary=item.boundary,
            )
            for item in result.parameters
        ],
        entities=[
            SemanticEntity(
                id=symbols.resolve("entity", item.symbol),
                meaning=item.meaning,
            )
            for item in sources.entities
        ],
        grain=[
            symbols.resolve("output", item)
            for item in result.grain_output_symbols
        ],
        outputs=[
            SemanticOutput(
                id=symbols.resolve("output", item.symbol),
                name=item.name,
                meaning=item.meaning,
                logical_type=item.logical_type,
                nullable=item.nullable,
            )
            for item in result.outputs
        ],
        filters=[
            SemanticFilter(
                id=symbols.resolve("filter", item.symbol),
                meaning=item.meaning,
                field_ids=[symbols.resolve("source", item.source_symbol)],
                parameter_ids=[
                    symbols.resolve("parameter", value)
                    for value in item.parameter_symbols
                ],
                operator=item.operator,
                literal_values=item.literal_values,
                skip_when_parameter_null=item.skip_when_parameter_null,
            )
            for item in sources.filters
        ],
        source_fields=[
            SemanticSourceField(
                id=symbols.resolve("source", item.symbol),
                entity_id=symbols.resolve("entity", item.entity_symbol),
                meaning=item.meaning,
                logical_type=item.logical_type,
                nullable=item.nullable,
            )
            for item in sources.fields
        ],
        facts=semantic_facts,
        fact_joins=[
            SemanticFactJoin(
                id=item.symbol,
                keys=[
                    SemanticFactJoinKey(
                        left=SemanticFactValueRef(
                            fact_id=symbols.resolve(
                                "fact", item.left_fact_symbol,
                            ),
                            value_id=symbols.resolve_fact_value(
                                item.left_fact_symbol,
                                item.left_dimension_symbol,
                            )[1],
                        ),
                        right=SemanticFactValueRef(
                            fact_id=symbols.resolve(
                                "fact", item.right_fact_symbol,
                            ),
                            value_id=symbols.resolve_fact_value(
                                item.right_fact_symbol,
                                item.right_dimension_symbol,
                            )[1],
                        ),
                    )
                ],
                join_type=item.join_type,
                meaning=item.meaning,
            )
            for item in blueprint.joins
        ],
        result_bindings=list(result_bindings.values()),
        result_filter=(
            _result_expression(expressions.result_filter, symbols)
            if expressions.result_filter is not None else None
        ),
        allow_empty=result.allow_empty,
        money_tolerance=result.money_tolerance,
    )
    symbol_table = symbols.model_dump()
    symbol_table["policy_coverage"] = coverage
    return contract, symbol_table
