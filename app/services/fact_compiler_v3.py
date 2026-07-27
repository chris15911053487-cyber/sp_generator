"""把冻结的业务事实确定性编译为物理计划，并组合多来源 Expected。"""

from __future__ import annotations

from decimal import Decimal

from app.contracts.relational_plan import (
    Expression,
    NamedExpression,
    PlanNode,
    RelationalPlan,
    ResultColumn,
)
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract, SemanticFact


class FactCompileError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


def _and(expressions: list[Expression]) -> Expression:
    if not expressions:
        raise FactCompileError("FACT_PREDICATE_EMPTY", "条件表达式为空")
    result = expressions[0]
    for item in expressions[1:]:
        result = Expression(
            kind="binary", operator="AND", args=[result, item],
        )
    return result


def _literal(value) -> Expression:
    if value is None:
        value_type = "null"
    elif isinstance(value, bool):
        value_type = "boolean"
    elif isinstance(value, int):
        value_type = "integer"
    elif isinstance(value, (float, Decimal)):
        value_type = "decimal"
    else:
        value_type = "string"
    return Expression(kind="literal", value=value, value_type=value_type)


def _entity_input(fact: SemanticFact, binding: SchemaBinding) -> PlanNode:
    wanted = set(fact.entity_ids)
    if len(wanted) == 1:
        entity_id = next(iter(wanted))
        return PlanNode(
            node_id=f"scan_{fact.id}_{entity_id}",
            kind="scan",
            entity_id=entity_id,
        )
    fields = {item.binding_id: item for item in binding.fields}
    grouped = {}
    for join in binding.joins:
        pair = frozenset({join.left_entity, join.right_entity})
        if pair.issubset(wanted):
            grouped.setdefault(pair, []).append(join)
    first = fact.entity_ids[0]
    root = PlanNode(
        node_id=f"scan_{fact.id}_{first}",
        kind="scan",
        entity_id=first,
    )
    included = {first}
    remaining = set(fact.entity_ids[1:])
    join_index = 0
    while remaining:
        candidate = next(
            (
                (entity, pair, joins)
                for entity in sorted(remaining)
                for pair, joins in grouped.items()
                if entity in pair and bool((set(pair) - {entity}) & included)
            ),
            None,
        )
        if candidate is None:
            raise FactCompileError(
                "FACT_ENTITY_GRAPH_DISCONNECTED",
                f"事实 {fact.id} 的实体无法由冻结 SchemaBinding 连接",
                evidence={"entities": sorted(wanted)},
            )
        entity, _pair, joins = candidate
        join_types = {item.join_type for item in joins}
        if len(join_types) != 1:
            raise FactCompileError(
                "FACT_JOIN_TYPE_CONFLICT",
                f"事实 {fact.id} 的实体关联类型冲突",
            )
        predicates = []
        for join in joins:
            predicates.append(
                Expression(
                    kind="binary",
                    operator="=",
                    args=[
                        Expression(
                            kind="column",
                            field_binding_id=join.left_field_binding_id,
                        ),
                        Expression(
                            kind="column",
                            field_binding_id=join.right_field_binding_id,
                        ),
                    ],
                )
            )
        join_index += 1
        root = PlanNode(
            node_id=f"join_{fact.id}_{join_index}",
            kind="join",
            left=root,
            right=PlanNode(
                node_id=f"scan_{fact.id}_{entity}",
                kind="scan",
                entity_id=entity,
            ),
            join_type=next(iter(join_types)),
            on=_and(predicates),
        )
        included.add(entity)
        remaining.remove(entity)
    return root


def _filter_expression(
    contract: SemanticContract,
    binding: SchemaBinding,
    filter_id: str,
) -> Expression:
    semantic_filter = next(
        item for item in contract.filters if item.id == filter_id
    )
    if len(semantic_filter.field_ids) != 1:
        raise FactCompileError(
            "FACT_FILTER_FIELD_AMBIGUOUS",
            f"过滤 {filter_id} 必须唯一定位逻辑字段",
        )
    candidates = [
        item for item in binding.fields
        if item.semantic_id == semantic_filter.field_ids[0]
    ]
    if len(candidates) != 1:
        raise FactCompileError(
            "FACT_FILTER_BINDING_AMBIGUOUS",
            f"过滤 {filter_id} 无法唯一定位物理字段",
        )
    field = candidates[0]
    column = Expression(kind="column", field_binding_id=field.binding_id)
    if semantic_filter.operator == "full_day_range":
        start, end = semantic_filter.parameter_ids
        return _and([
            Expression(
                kind="binary",
                operator=">=",
                args=[
                    column,
                    Expression(kind="parameter", parameter_id=start),
                ],
            ),
            Expression(
                kind="binary",
                operator="<",
                args=[
                    column,
                    Expression(
                        kind="function",
                        operator="DATEADD",
                        args=[
                            _literal("day"),
                            _literal(1),
                            Expression(kind="parameter", parameter_id=end),
                        ],
                    ),
                ],
            ),
        ])
    if semantic_filter.operator == "between":
        start, end = semantic_filter.parameter_ids
        return _and([
            Expression(
                kind="binary",
                operator=">=",
                args=[
                    column,
                    Expression(kind="parameter", parameter_id=start),
                ],
            ),
            Expression(
                kind="binary",
                operator="<=",
                args=[
                    column,
                    Expression(kind="parameter", parameter_id=end),
                ],
            ),
        ])
    if semantic_filter.operator in {"is_null", "is_not_null"}:
        return Expression(
            kind="unary",
            operator=(
                "IS NULL"
                if semantic_filter.operator == "is_null" else "IS NOT NULL"
            ),
            args=[column],
        )
    operators = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "like": "LIKE",
    }
    values = [
        Expression(kind="parameter", parameter_id=item)
        for item in semantic_filter.parameter_ids
    ] + [
        _literal(field.literal_map.get(str(item), item))
        for item in semantic_filter.literal_values
    ]
    if semantic_filter.operator not in operators or len(values) != 1:
        raise FactCompileError(
            "FACT_FILTER_SHAPE_UNSUPPORTED",
            f"过滤 {filter_id} 无法确定性编译",
        )
    comparison = Expression(
        kind="binary",
        operator=operators[semantic_filter.operator],
        args=[column, values[0]],
    )
    if not semantic_filter.skip_when_parameter_null:
        return comparison
    parameter_id = semantic_filter.parameter_ids[0]
    return Expression(
        kind="binary",
        operator="OR",
        args=[
            Expression(
                kind="unary",
                operator="IS NULL",
                args=[
                    Expression(
                        kind="parameter",
                        parameter_id=parameter_id,
                    )
                ],
            ),
            comparison,
        ],
    )


def _source_field_ids(expression) -> list[str]:
    if expression is None:
        return []
    result = [expression.field_id] if expression.field_id else []
    for child in expression.args:
        result.extend(_source_field_ids(child))
    for case in expression.cases:
        result.extend(_source_field_ids(case.when))
        result.extend(_source_field_ids(case.then))
    result.extend(_source_field_ids(expression.else_expr))
    return result


def _source_expression(expression, fields) -> Expression:
    if expression.kind == "field":
        candidates = fields.get(expression.field_id, [])
        if len(candidates) != 1:
            raise FactCompileError(
                "FACT_FIELD_BINDING_AMBIGUOUS",
                f"源表达式字段 {expression.field_id} 无法唯一绑定",
            )
        return Expression(
            kind="column",
            field_binding_id=candidates[0].binding_id,
        )
    if expression.kind == "literal":
        return _literal(expression.value)
    if expression.kind in {"binary", "unary", "function"}:
        return Expression(
            kind=expression.kind,
            operator=expression.operator,
            args=[
                _source_expression(item, fields)
                for item in expression.args
            ],
        )
    if expression.kind == "case":
        from app.contracts.relational_plan import WhenThen

        return Expression(
            kind="case",
            cases=[
                WhenThen(
                    when=_source_expression(item.when, fields),
                    then=_source_expression(item.then, fields),
                )
                for item in expression.cases
            ],
            else_expr=(
                _source_expression(expression.else_expr, fields)
                if expression.else_expr is not None else None
            ),
        )
    raise FactCompileError(
        "FACT_SOURCE_EXPRESSION_UNSUPPORTED",
        f"不支持源表达式 {expression.kind}",
    )


def compile_fact_plan(
    contract: SemanticContract,
    binding: SchemaBinding,
    fact: SemanticFact,
    *,
    prefix: str = "",
) -> RelationalPlan:
    fields = {}
    for item in binding.fields:
        fields.setdefault(item.semantic_id, []).append(item)
    required = [
        item.field_id for item in fact.dimensions
        if item.field_id is not None
    ] + [
        item.field_id for item in fact.measures if item.field_id is not None
    ]
    for measure in fact.measures:
        required.extend(_source_field_ids(measure.expression))
    for dimension in fact.dimensions:
        required.extend(_source_field_ids(dimension.expression))
    ambiguous = [
        field_id for field_id in required
        if len(fields.get(field_id, [])) != 1
    ]
    if ambiguous:
        raise FactCompileError(
            "FACT_FIELD_BINDING_AMBIGUOUS",
            f"事实 {fact.id} 的源字段无法唯一绑定",
            evidence={"fields": sorted(set(ambiguous))},
        )
    root = _entity_input(fact, binding)
    if fact.filter_ids:
        root = PlanNode(
            node_id=f"filter_{fact.id}",
            kind="filter",
            input=root,
            predicate=_and([
                _filter_expression(contract, binding, item)
                for item in fact.filter_ids
            ]),
        )

    source_fields = {item.id: item for item in contract.source_fields}

    def name(value_id: str) -> str:
        return prefix + value_id

    def dimension_expression(item) -> Expression:
        if item.expression is not None:
            return _source_expression(item.expression, fields)
        expression = Expression(
            kind="column",
            field_binding_id=fields[item.field_id][0].binding_id,
        )
        if source_fields[item.field_id].logical_type == "date":
            return Expression(
                kind="cast",
                target_type="date",
                args=[expression],
            )
        return expression

    dimensions = [
        NamedExpression(
            name=name(item.id),
            expression=dimension_expression(item),
        )
        for item in fact.dimensions
    ]
    aggregate_mode = any(
        item.aggregation != "none" for item in fact.measures
    )
    if aggregate_mode and any(
        item.aggregation == "none" for item in fact.measures
    ):
        raise FactCompileError(
            "FACT_AGGREGATION_MIXED",
            f"事实 {fact.id} 不能混用聚合与非聚合指标",
        )
    if aggregate_mode:
        aggregates = []
        for item in fact.measures:
            if item.aggregation == "count_rows":
                expression = Expression(
                    kind="function",
                    operator="COUNT",
                    args=[_literal(1)],
                )
            else:
                operator = {
                    "sum": "SUM",
                    "count_distinct": "COUNT_DISTINCT",
                    "min": "MIN",
                    "max": "MAX",
                    "avg": "AVG",
                }[item.aggregation]
                measure_input = (
                    _source_expression(item.expression, fields)
                    if item.expression is not None
                    else Expression(
                        kind="column",
                        field_binding_id=fields[
                            item.field_id
                        ][0].binding_id,
                    )
                )
                expression = Expression(
                    kind="function",
                    operator=operator,
                    args=[measure_input],
                )
            aggregates.append(
                NamedExpression(name=name(item.id), expression=expression)
            )
        root = PlanNode(
            node_id=f"aggregate_{fact.id}",
            kind="aggregate",
            input=root,
            group_by=dimensions,
            aggregates=aggregates,
        )
    else:
        measures = [
            NamedExpression(
                name=name(item.id),
                expression=(
                    _source_expression(item.expression, fields)
                    if item.expression is not None
                    else Expression(
                        kind="column",
                        field_binding_id=fields[
                            item.field_id
                        ][0].binding_id,
                    )
                ),
            )
            for item in fact.measures
        ]
        root = PlanNode(
            node_id=f"project_{fact.id}",
            kind="project",
            input=root,
            projections=dimensions + measures,
        )
    result_schema = [
        ResultColumn(
            name=name(item.id),
            logical_type=(
                item.logical_type
                if item.expression is not None
                else source_fields[item.field_id].logical_type
            ),
            nullable=(
                True
                if (
                    item.expression is not None
                    or source_fields[item.field_id].logical_type == "date"
                )
                else source_fields[item.field_id].nullable
            ),
        )
        for item in fact.dimensions
    ] + [
        ResultColumn(
            name=name(item.id),
            logical_type=item.logical_type,
            nullable=True,
        )
        for item in fact.measures
    ]
    return RelationalPlan(
        plan_id=f"fact_{fact.id}",
        purpose=fact.meaning,
        root=root,
        result_schema=result_schema,
    )


def _fact_output_name(fact_id: str, value_id: str) -> str:
    return f"{fact_id}__{value_id}"


def _result_expression(
    value,
    binding_by_output: dict | None = None,
    resolving: tuple[str, ...] = (),
) -> Expression:
    if value.kind == "fact_value":
        return Expression(
            kind="output",
            output_name=_fact_output_name(
                value.fact_value.fact_id,
                value.fact_value.value_id,
            ),
        )
    if value.kind == "output":
        output_id = value.output_id
        if binding_by_output is None or output_id not in binding_by_output:
            raise FactCompileError(
                "FACT_RESULT_OUTPUT_UNKNOWN",
                f"结果公式引用未知输出 {output_id}",
            )
        if output_id in resolving:
            raise FactCompileError(
                "FACT_RESULT_OUTPUT_CYCLE",
                f"结果公式存在循环输出依赖 {output_id}",
            )
        return _result_expression(
            binding_by_output[output_id].expression,
            binding_by_output,
            resolving + (output_id,),
        )
    if value.kind == "parameter":
        return Expression(
            kind="parameter",
            parameter_id=value.parameter_id,
        )
    if value.kind == "literal":
        return _literal(value.value)
    if value.kind in {"binary", "unary", "function"}:
        return Expression(
            kind=value.kind,
            operator=value.operator,
            args=[
                _result_expression(
                    item, binding_by_output, resolving,
                )
                for item in value.args
            ],
        )
    if value.kind == "case":
        from app.contracts.relational_plan import WhenThen

        return Expression(
            kind="case",
            cases=[
                WhenThen(
                    when=_result_expression(
                        item.when, binding_by_output, resolving,
                    ),
                    then=_result_expression(
                        item.then, binding_by_output, resolving,
                    ),
                )
                for item in value.cases
            ],
            else_expr=(
                _result_expression(
                    value.else_expr, binding_by_output, resolving,
                )
                if value.else_expr is not None else None
            ),
        )
    raise FactCompileError(
        "FACT_RESULT_EXPRESSION_UNSUPPORTED",
        f"不支持结果表达式 {value.kind}",
    )


def compile_contract_plan(
    contract: SemanticContract,
    binding: SchemaBinding,
) -> RelationalPlan:
    if not contract.facts:
        raise FactCompileError("FACTS_MISSING", "合同没有结构化 facts")
    plans = {
        fact.id: compile_fact_plan(
            contract,
            binding,
            fact,
            prefix=f"{fact.id}__",
        )
        for fact in contract.facts
    }
    first = contract.facts[0].id
    root = plans[first].root
    included = {first}
    remaining = {item.id for item in contract.facts[1:]}
    join_index = 0
    while remaining:
        candidate = next(
            (
                join for join in contract.fact_joins
                if {
                    join.keys[0].left.fact_id,
                    join.keys[0].right.fact_id,
                } & included
                and {
                    join.keys[0].left.fact_id,
                    join.keys[0].right.fact_id,
                } & remaining
            ),
            None,
        )
        if candidate is None:
            raise FactCompileError(
                "FACT_GRAPH_DISCONNECTED",
                "多事实匹配图不连通",
            )
        pair = {
            candidate.keys[0].left.fact_id,
            candidate.keys[0].right.fact_id,
        }
        new_fact = next(iter(pair & remaining))
        predicates = [
            Expression(
                kind="binary",
                operator="=",
                args=[
                    Expression(
                        kind="output",
                        output_name=_fact_output_name(
                            key.left.fact_id, key.left.value_id,
                        ),
                    ),
                    Expression(
                        kind="output",
                        output_name=_fact_output_name(
                            key.right.fact_id, key.right.value_id,
                        ),
                    ),
                ],
            )
            for key in candidate.keys
        ]
        join_index += 1
        root = PlanNode(
            node_id=f"join_facts_{join_index}",
            kind="join",
            left=root,
            right=plans[new_fact].root,
            join_type=candidate.join_type,
            on=_and(predicates),
        )
        included.add(new_fact)
        remaining.remove(new_fact)
    binding_by_output = {
        item.output_id: item for item in contract.result_bindings
    }
    if contract.result_filter is not None:
        root = PlanNode(
            node_id="filter_contract_result",
            kind="filter",
            input=root,
            predicate=_result_expression(
                contract.result_filter, binding_by_output,
            ),
        )
    root = PlanNode(
        node_id="project_contract_result",
        kind="project",
        input=root,
        projections=[
            NamedExpression(
                name=output.name,
                expression=_result_expression(
                    binding_by_output[output.id].expression,
                    binding_by_output,
                    (output.id,),
                ),
            )
            for output in contract.outputs
        ],
    )
    return RelationalPlan(
        plan_id="facts_" + contract.contract_id.replace(":", "_"),
        purpose=contract.purpose,
        root=root,
        result_schema=[
            ResultColumn(
                name=item.name,
                logical_type=item.logical_type,
                nullable=item.nullable,
            )
            for item in contract.outputs
        ],
    )


def compose_expected_rows(
    contract: SemanticContract,
    fact_rows: dict[str, list[dict]],
    parameters: dict | None = None,
) -> list[dict]:
    """按冻结事实连接和结果表达式在内存中组合最终 Expected。"""
    combined = [
        {
            (first.id, key): value
            for key, value in row.items()
        }
        for first in contract.facts[:1]
        for row in fact_rows[first.id]
    ]
    included = {contract.facts[0].id}
    remaining = {item.id for item in contract.facts[1:]}
    while remaining:
        join = next(
            (
                item for item in contract.fact_joins
                if {
                    item.keys[0].left.fact_id,
                    item.keys[0].right.fact_id,
                } & included
                and {
                    item.keys[0].left.fact_id,
                    item.keys[0].right.fact_id,
                } & remaining
            ),
            None,
        )
        if join is None:
            raise FactCompileError(
                "FACT_GRAPH_DISCONNECTED", "多事实匹配图不连通",
            )
        pair = {
            join.keys[0].left.fact_id,
            join.keys[0].right.fact_id,
        }
        new_fact = next(iter(pair & remaining))
        new_rows = [
            {(new_fact, key): value for key, value in row.items()}
            for row in fact_rows[new_fact]
        ]
        matched_right = set()
        next_rows = []
        for left_row in combined:
            matches = []
            for index, right_row in enumerate(new_rows):
                match = True
                for key in join.keys:
                    left_key = (
                        key.left.fact_id, key.left.value_id,
                    )
                    right_key = (
                        key.right.fact_id, key.right.value_id,
                    )
                    left_value = (
                        left_row[left_key]
                        if left_key in left_row else right_row.get(left_key)
                    )
                    right_value = (
                        left_row[right_key]
                        if right_key in left_row else right_row.get(right_key)
                    )
                    if (
                        left_value is None
                        or right_value is None
                        or left_value != right_value
                    ):
                        match = False
                        break
                if match:
                    matches.append((index, right_row))
            if matches:
                for index, right_row in matches:
                    matched_right.add(index)
                    next_rows.append({**left_row, **right_row})
            elif join.join_type in {"left", "full"}:
                next_rows.append(left_row)
        if join.join_type == "full":
            next_rows.extend(
                row for index, row in enumerate(new_rows)
                if index not in matched_right
            )
        combined = next_rows
        included.add(new_fact)
        remaining.remove(new_fact)

    def evaluate(expression, row):
        if expression.kind == "fact_value":
            return row.get((
                expression.fact_value.fact_id,
                expression.fact_value.value_id,
            ))
        if expression.kind == "output":
            return evaluate(
                result_bindings[expression.output_id],
                row,
            )
        if expression.kind == "parameter":
            return (parameters or {}).get(expression.parameter_id)
        if expression.kind == "literal":
            return expression.value
        args = [evaluate(item, row) for item in expression.args]
        if expression.kind == "binary":
            left, right = args
            operator = str(expression.operator).upper()
            if operator == "AND":
                return bool(left) and bool(right)
            if operator == "OR":
                return bool(left) or bool(right)
            if left is None or right is None:
                return None
            return {
                "+": lambda: left + right,
                "-": lambda: left - right,
                "*": lambda: left * right,
                "/": lambda: left / right,
                "=": lambda: left == right,
                "<>": lambda: left != right,
                ">": lambda: left > right,
                ">=": lambda: left >= right,
                "<": lambda: left < right,
                "<=": lambda: left <= right,
            }[operator]()
        if expression.kind == "unary":
            operator = str(expression.operator).upper()
            value = args[0]
            if operator == "IS NULL":
                return value is None
            if operator == "IS NOT NULL":
                return value is not None
            if operator == "NOT":
                return not bool(value)
            if operator == "NEGATE":
                return None if value is None else -value
            raise FactCompileError(
                "FACT_RESULT_UNARY_UNSUPPORTED",
                f"不支持内存结果一元运算符 {operator}",
            )
        if expression.kind == "function":
            operator = str(expression.operator).upper()
            if operator == "COALESCE":
                return next((item for item in args if item is not None), None)
            if operator == "ABS":
                return None if args[0] is None else abs(args[0])
            if operator == "NULLIF":
                return None if args[0] == args[1] else args[0]
            raise FactCompileError(
                "FACT_RESULT_FUNCTION_UNSUPPORTED",
                f"不支持内存结果函数 {operator}",
            )
        if expression.kind == "case":
            for item in expression.cases:
                if evaluate(item.when, row):
                    return evaluate(item.then, row)
            return (
                evaluate(expression.else_expr, row)
                if expression.else_expr is not None else None
            )
        raise FactCompileError(
            "FACT_RESULT_EXPRESSION_UNSUPPORTED",
            f"不支持内存结果表达式 {expression.kind}",
        )

    result_bindings = {
        item.output_id: item.expression for item in contract.result_bindings
    }
    source_rows = (
        [
            row for row in combined
            if evaluate(contract.result_filter, row)
        ]
        if contract.result_filter is not None
        else combined
    )
    return [
        {
            output.name: evaluate(result_bindings[output.id], row)
            for output in contract.outputs
        }
        for row in source_rows
    ]
