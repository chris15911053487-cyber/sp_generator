"""证明关系计划覆盖了机器可读业务语义，而不只是在语法上合法。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.contracts.relational_plan import Expression, PlanNode, RelationalPlan
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract, SemanticFilter


class PlanSemanticError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


@dataclass
class _ExpressionEvidence:
    field_binding_ids: set[str]
    parameter_ids: set[str]
    literals: list[Any]
    binary_operators: list[str]
    unary_operators: list[str]


def _nodes(node: PlanNode) -> list[PlanNode]:
    result = [node]
    for child in (node.input, node.left, node.right):
        if child is not None:
            result.extend(_nodes(child))
    for child in node.inputs:
        result.extend(_nodes(child))
    return result


def _evidence(expression: Expression) -> _ExpressionEvidence:
    fields: set[str] = set()
    parameters: set[str] = set()
    literals: list[Any] = []
    binary: list[str] = []
    unary: list[str] = []

    def visit(item: Expression):
        if item.kind == "column" and item.field_binding_id:
            fields.add(item.field_binding_id)
        elif item.kind == "parameter" and item.parameter_id:
            parameters.add(item.parameter_id)
        elif item.kind == "literal":
            literals.append(item.value)
        elif item.kind == "binary" and item.operator:
            binary.append(item.operator.upper())
        elif item.kind == "unary" and item.operator:
            unary.append(item.operator.upper())
        for child in item.args:
            visit(child)
        for case in item.cases:
            visit(case.when)
            visit(case.then)
        if item.else_expr is not None:
            visit(item.else_expr)

    visit(expression)
    return _ExpressionEvidence(fields, parameters, literals, binary, unary)


def _contains_full_day_range(
    expression: Expression,
    start_parameter: str,
    end_parameter: str,
    required_fields: set[str],
    binding_semantics: dict[str, str],
) -> bool:
    comparisons: list[Expression] = []

    def visit(item: Expression):
        if item.kind == "binary":
            comparisons.append(item)
        for child in item.args:
            visit(child)
        for case in item.cases:
            visit(case.when)
            visit(case.then)
        if item.else_expr is not None:
            visit(item.else_expr)

    visit(expression)
    def left_matches(item: Expression) -> bool:
        if len(item.args) != 2:
            return False
        left = _evidence(item.args[0])
        semantics = {
            binding_semantics[value]
            for value in left.field_binding_ids
            if value in binding_semantics
        }
        return required_fields.issubset(semantics)

    lower = any(
        str(item.operator).upper() == ">="
        and left_matches(item)
        and item.args[1].kind == "parameter"
        and item.args[1].parameter_id == start_parameter
        for item in comparisons
    )
    upper = False
    for item in comparisons:
        if (
            str(item.operator).upper() != "<"
            or len(item.args) != 2
            or not left_matches(item)
        ):
            continue
        right = item.args[1]
        if (
            right.kind == "function"
            and str(right.operator).upper() == "DATEADD"
            and len(right.args) == 3
            and right.args[0].kind == "literal"
            and str(right.args[0].value).casefold() == "day"
            and right.args[1].kind == "literal"
            and right.args[1].value == 1
            and right.args[2].kind == "parameter"
            and right.args[2].parameter_id == end_parameter
        ):
            upper = True
    return lower and upper


def _filter_matches(
    semantic_filter: SemanticFilter,
    expression: Expression,
    binding_semantics: dict[str, str],
    literal_maps: dict[str, dict[str, Any]],
) -> bool:
    evidence = _evidence(expression)
    semantic_fields = {
        binding_semantics[item]
        for item in evidence.field_binding_ids
        if item in binding_semantics
    }
    if not set(semantic_filter.field_ids).issubset(semantic_fields):
        return False
    if not set(semantic_filter.parameter_ids).issubset(evidence.parameter_ids):
        return False
    expected_literals = []
    for value in semantic_filter.literal_values:
        mapped = next(
            (
                literal_maps.get(field_id, {}).get(str(value))
                for field_id in semantic_filter.field_ids
                if str(value) in literal_maps.get(field_id, {})
            ),
            value,
        )
        expected_literals.append(mapped)
    if any(value not in evidence.literals for value in expected_literals):
        return False
    if (
        semantic_filter.skip_when_parameter_null
        and not _contains_optional_parameter_bypass(
            expression,
            semantic_filter.parameter_ids[0],
        )
    ):
        return False
    operator = semantic_filter.operator
    if operator == "full_day_range":
        return _contains_full_day_range(
            expression,
            semantic_filter.parameter_ids[0],
            semantic_filter.parameter_ids[1],
            set(semantic_filter.field_ids),
            binding_semantics,
        )
    if operator == "between":
        comparisons = _binary_expressions(expression)
        return (
            _direct_comparison_matches(
                comparisons,
                ">=",
                set(semantic_filter.field_ids),
                {semantic_filter.parameter_ids[0]},
                [],
                binding_semantics,
            )
            and _direct_comparison_matches(
                comparisons,
                "<=",
                set(semantic_filter.field_ids),
                {semantic_filter.parameter_ids[1]},
                [],
                binding_semantics,
            )
        )
    if operator == "is_null":
        return "IS NULL" in evidence.unary_operators
    if operator == "is_not_null":
        return "IS NOT NULL" in evidence.unary_operators
    expected = {
        "eq": "=",
        "ne": "<>",
        "gt": ">",
        "gte": ">=",
        "lt": "<",
        "lte": "<=",
        "like": "LIKE",
    }[operator]
    return _direct_comparison_matches(
        _binary_expressions(expression),
        expected,
        set(semantic_filter.field_ids),
        set(semantic_filter.parameter_ids),
        expected_literals,
        binding_semantics,
        symmetric=operator in {"eq", "ne"},
    )


def _contains_optional_parameter_bypass(
    expression: Expression,
    parameter_id: str,
) -> bool:
    if (
        expression.kind == "binary"
        and str(expression.operator).upper() == "OR"
        and len(expression.args) == 2
    ):
        for child in expression.args:
            child_evidence = _evidence(child)
            if (
                "IS NULL" in child_evidence.unary_operators
                and parameter_id in child_evidence.parameter_ids
            ):
                return True
    return any(
        _contains_optional_parameter_bypass(child, parameter_id)
        for child in expression.args
    )


def _binary_expressions(expression: Expression) -> list[Expression]:
    result = [expression] if expression.kind == "binary" else []
    for child in expression.args:
        result.extend(_binary_expressions(child))
    return result


def _direct_comparison_matches(
    comparisons: list[Expression],
    operator: str,
    required_fields: set[str],
    required_parameters: set[str],
    required_literals: list[Any],
    binding_semantics: dict[str, str],
    *,
    symmetric: bool = False,
) -> bool:
    def side_evidence(item: Expression):
        value = _evidence(item)
        semantics = {
            binding_semantics[field]
            for field in value.field_binding_ids
            if field in binding_semantics
        }
        return value, semantics

    for comparison in comparisons:
        if (
            str(comparison.operator).upper() != operator
            or len(comparison.args) != 2
        ):
            continue
        orientations = [(comparison.args[0], comparison.args[1])]
        if symmetric:
            orientations.append((comparison.args[1], comparison.args[0]))
        for field_side, value_side in orientations:
            _, field_semantics = side_evidence(field_side)
            value_evidence, _ = side_evidence(value_side)
            if not required_fields.issubset(field_semantics):
                continue
            if not required_parameters.issubset(value_evidence.parameter_ids):
                continue
            if any(
                value not in value_evidence.literals
                for value in required_literals
            ):
                continue
            return True
    return False


def _join_pairs(expression: Expression) -> list[frozenset[str]]:
    pairs = []
    if (
        expression.kind == "binary"
        and str(expression.operator).upper() == "="
        and len(expression.args) == 2
        and expression.args[0].kind == "column"
        and expression.args[1].kind == "column"
    ):
        pairs.append(
            frozenset(
                {
                    str(expression.args[0].field_binding_id),
                    str(expression.args[1].field_binding_id),
                }
            )
        )
    for child in expression.args:
        pairs.extend(_join_pairs(child))
    return pairs


def _output_definitions(node: PlanNode) -> list[dict[str, Expression]]:
    if node.kind == "sort" and node.input is not None:
        return _output_definitions(node.input)
    if node.kind == "union_all":
        result = []
        for child in node.inputs:
            result.extend(_output_definitions(child))
        return result
    if node.kind == "project":
        return [{item.name.casefold(): item.expression for item in node.projections}]
    if node.kind == "aggregate":
        return [
            {
                item.name.casefold(): item.expression
                for item in node.group_by + node.aggregates
            }
        ]
    return []


def _output_definition_contexts(
    node: PlanNode,
) -> list[tuple[dict[str, Expression], PlanNode | None]]:
    """返回最终输出定义及其输入节点，用于沿嵌套 project 追踪字段血缘。"""
    if node.kind == "sort" and node.input is not None:
        return _output_definition_contexts(node.input)
    if node.kind == "union_all":
        result = []
        for child in node.inputs:
            result.extend(_output_definition_contexts(child))
        return result
    if node.kind == "project":
        return [
            (
                {
                    item.name.casefold(): item.expression
                    for item in node.projections
                },
                node.input,
            )
        ]
    if node.kind == "aggregate":
        return [
            (
                {
                    item.name.casefold(): item.expression
                    for item in node.group_by + node.aggregates
                },
                node.input,
            )
        ]
    return []


def _lineage_field_binding_ids(
    expression: Expression,
    source: PlanNode | None,
    *,
    visited: set[tuple[int, str]] | None = None,
) -> set[str]:
    """沿 output 别名递归到真实 column 绑定；循环或无来源别名不猜测。"""
    result = set(_evidence(expression).field_binding_ids)
    output_names: set[str] = set()

    def collect(item: Expression):
        if item.kind == "output" and item.output_name:
            output_names.add(item.output_name.casefold())
        for child in item.args:
            collect(child)
        for case in item.cases:
            collect(case.when)
            collect(case.then)
        if item.else_expr is not None:
            collect(item.else_expr)

    collect(expression)
    if source is None or not output_names:
        return result
    seen = visited or set()
    for definitions, upstream in _output_definition_contexts(source):
        for output_name in output_names:
            marker = (id(source), output_name)
            target = definitions.get(output_name)
            if target is None or marker in seen:
                continue
            result.update(
                _lineage_field_binding_ids(
                    target,
                    upstream,
                    visited=seen | {marker},
                )
            )
    return result


def validate_plan_semantics(
    plan: RelationalPlan,
    contract: SemanticContract,
    binding: SchemaBinding,
    *,
    output_projection: list[str] | None = None,
    allow_entity_subset: bool = False,
) -> None:
    nodes = _nodes(plan.root)
    scans = {
        str(node.entity_id) for node in nodes if node.kind == "scan"
    }
    required_entities = {item.id for item in contract.entities}
    entities_valid = (
        bool(scans)
        and scans.issubset(required_entities)
        if allow_entity_subset else scans == required_entities
    )
    if not entities_valid:
        raise PlanSemanticError(
            "PLAN_ENTITY_COVERAGE_MISMATCH",
            "关系计划使用的业务实体与语义合同不一致",
            evidence={
                "missing": sorted(required_entities - scans),
                "extra": sorted(scans - required_entities),
            },
        )

    binding_semantics = {
        item.binding_id: item.semantic_id for item in binding.fields
    }
    literal_maps: dict[str, dict[str, Any]] = {}
    for item in binding.fields:
        literal_maps.setdefault(item.semantic_id, {}).update(item.literal_map)
    predicates = [
        node.predicate for node in nodes
        if node.kind == "filter" and node.predicate is not None
    ]
    field_entities = {
        item.semantic_id: item.entity_id for item in binding.fields
    }
    applicable_filters = [
        item for item in contract.filters
        if any(field_entities.get(field_id) in scans for field_id in item.field_ids)
    ]
    missing_filters = [
        item.id for item in applicable_filters
        if not any(
            _filter_matches(
                item, predicate, binding_semantics, literal_maps,
            )
            for predicate in predicates
        )
    ]
    if missing_filters:
        missing_set = set(missing_filters)
        expected_filters = [
            {
                "filter_id": item.id,
                "operator": item.operator,
                "field_ids": item.field_ids,
                "parameter_ids": item.parameter_ids,
                "semantic_literals": item.literal_values,
                "physical_literals": [
                    next(
                        (
                            literal_maps.get(field_id, {}).get(str(value))
                            for field_id in item.field_ids
                            if str(value) in literal_maps.get(field_id, {})
                        ),
                        value,
                    )
                    for value in item.literal_values
                ],
            }
            for item in applicable_filters
            if item.id in missing_set
        ]
        raise PlanSemanticError(
            "PLAN_FILTER_COVERAGE_MISSING",
            "关系计划没有覆盖全部结构化业务过滤条件",
            evidence={
                "missing_filters": missing_filters,
                "expected_filters": expected_filters,
            },
        )

    declared_joins = {
        (
            frozenset(
                {
                    item.left_field_binding_id,
                    item.right_field_binding_id,
                }
            ),
            item.join_type,
        )
        for item in binding.joins
    }
    actual_joins = set()
    for node in nodes:
        if node.kind != "join" or node.on is None:
            continue
        for pair in _join_pairs(node.on):
            actual_joins.add((pair, node.join_type))
    applicable_declared_joins = {
        item for item in declared_joins
        if all(
            binding.field(field_id).entity_id in scans
            for field_id in item[0]
        )
    }
    if actual_joins != applicable_declared_joins:
        def serializable(values):
            return sorted(
                [
                    {"fields": sorted(pair), "join_type": join_type}
                    for pair, join_type in values
                ],
                key=lambda item: (item["join_type"], item["fields"]),
            )

        raise PlanSemanticError(
            "PLAN_JOIN_COVERAGE_MISMATCH",
            "关系计划中的 JOIN 与冻结 SchemaBinding 不一致",
            evidence={
                "expected": serializable(applicable_declared_joins),
                "actual": serializable(actual_joins),
            },
        )

    projected = (
        {item.casefold() for item in output_projection}
        if output_projection is not None else None
    )
    expected_outputs = [
        item for item in contract.outputs
        if projected is None or item.name.casefold() in projected
    ]
    expected_columns = [
        (item.name.casefold(), item.logical_type)
        for item in expected_outputs
    ]
    actual_columns = [
        (item.name.casefold(), item.logical_type)
        for item in plan.result_schema
    ]
    if actual_columns != expected_columns:
        raise PlanSemanticError(
            "PLAN_OUTPUT_CONTRACT_MISMATCH",
            "关系计划结果结构与语义合同不一致",
        )
    definition_contexts = _output_definition_contexts(plan.root)
    if not definition_contexts:
        raise PlanSemanticError(
            "PLAN_OUTPUT_DEFINITION_MISSING",
            "关系计划必须用 project 或 aggregate 明确定义最终输出",
        )
    derived_by_output = {
        item.output_id: item for item in contract.derived_fields
    }
    output_id_by_name = {
        item.name.casefold(): item.id for item in contract.outputs
    }

    def semantic_signature(expression):
        if expression.kind == "output":
            return ("output", expression.output_id)
        if expression.kind == "literal":
            return ("literal", expression.value)
        if expression.kind == "case":
            return (
                "case",
                tuple(
                    (
                        semantic_signature(item.when),
                        semantic_signature(item.then),
                    )
                    for item in expression.cases
                ),
                (
                    semantic_signature(expression.else_expr)
                    if expression.else_expr is not None else None
                ),
            )
        return (
            expression.kind,
            str(expression.operator).upper(),
            tuple(semantic_signature(item) for item in expression.args),
        )

    def plan_signature(expression):
        if expression.kind == "column":
            return (
                "output",
                binding_semantics.get(str(expression.field_binding_id)),
            )
        if expression.kind == "output":
            return (
                "output",
                output_id_by_name.get(str(expression.output_name).casefold()),
            )
        if expression.kind == "literal":
            return ("literal", expression.value)
        if expression.kind == "case":
            return (
                "case",
                tuple(
                    (
                        plan_signature(item.when),
                        plan_signature(item.then),
                    )
                    for item in expression.cases
                ),
                (
                    plan_signature(expression.else_expr)
                    if expression.else_expr is not None else None
                ),
            )
        if expression.kind in {"binary", "function"}:
            return (
                expression.kind,
                str(expression.operator).upper(),
                tuple(plan_signature(item) for item in expression.args),
            )
        return ("unsupported", expression.kind)

    for branch_index, (output_map, output_source) in enumerate(
        definition_contexts
    ):
        for output in expected_outputs:
            expression = output_map.get(output.name.casefold())
            if expression is None:
                raise PlanSemanticError(
                    "PLAN_OUTPUT_DEFINITION_MISSING",
                    f"关系计划缺少输出 {output.name}",
                )
            derived = derived_by_output.get(output.id)
            if derived is not None:
                if plan_signature(expression) != semantic_signature(
                    derived.expression
                ):
                    raise PlanSemanticError(
                        "PLAN_DERIVED_EXPRESSION_MISMATCH",
                        f"派生输出 {output.name} 与 SemanticContract 公式不一致",
                        evidence={"branch": branch_index},
                    )
                continue
            field_binding_ids = _lineage_field_binding_ids(
                expression, output_source,
            )
            semantics = {
                binding_semantics[field]
                for field in field_binding_ids
                if field in binding_semantics
            }
            if output.id not in semantics:
                raise PlanSemanticError(
                    "PLAN_OUTPUT_SOURCE_MISMATCH",
                    f"输出 {output.name} 没有使用其绑定的业务字段",
                    evidence={
                        "branch": branch_index,
                        "expected_semantic_id": output.id,
                        "actual_semantic_ids": sorted(semantics),
                    },
                )
