"""Restricted relational algebra used instead of arbitrary model-authored SQL."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract


ExpressionKind = Literal[
    "column", "output", "parameter", "literal", "binary", "unary", "function",
    "case", "cast",
]
NodeKind = Literal[
    "scan", "join", "filter", "project", "aggregate", "union_all", "sort",
]


class WhenThen(StrictContract):
    when: "Expression"
    then: "Expression"


class Expression(StrictContract):
    kind: ExpressionKind
    field_binding_id: str | None = None
    output_name: str | None = None
    parameter_id: str | None = None
    value: Any | None = None
    value_type: Literal[
        "null", "string", "integer", "decimal", "boolean", "date", "datetime",
    ] | None = None
    operator: str | None = None
    target_type: Literal[
        "string", "integer", "decimal", "money", "date", "datetime", "boolean",
    ] | None = None
    args: list["Expression"] = Field(default_factory=list)
    cases: list[WhenThen] = Field(default_factory=list)
    else_expr: "Expression | None" = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "column" and not self.field_binding_id:
            raise ValueError("column 表达式缺少 field_binding_id")
        if self.kind == "output" and not self.output_name:
            raise ValueError("output 表达式缺少 output_name")
        if self.kind == "parameter" and not self.parameter_id:
            raise ValueError("parameter 表达式缺少 parameter_id")
        if self.kind == "literal" and self.value_type is None:
            raise ValueError("literal 表达式缺少 value_type")
        if self.kind in {"binary", "unary", "function"} and not self.operator:
            raise ValueError(f"{self.kind} 表达式缺少 operator")
        if self.kind == "binary" and len(self.args) != 2:
            raise ValueError("binary 表达式必须恰好有两个参数")
        if self.kind == "unary" and len(self.args) != 1:
            raise ValueError("unary 表达式必须恰好有一个参数")
        if self.kind == "case" and not self.cases:
            raise ValueError("case 表达式至少需要一个 WHEN")
        if self.kind == "cast" and (
            len(self.args) != 1 or self.target_type is None
        ):
            raise ValueError("cast 表达式必须有一个参数和 target_type")
        if self.kind == "function" and not self.args:
            raise ValueError("function 表达式至少需要一个参数")
        allowed = {
            "column": {"field_binding_id"},
            "output": {"output_name"},
            "parameter": {"parameter_id"},
            "literal": {"value", "value_type"},
            "binary": {"operator", "args"},
            "unary": {"operator", "args"},
            "function": {"operator", "args"},
            "case": {"cases", "else_expr"},
            "cast": {"args", "target_type"},
        }[self.kind]
        values = {
            "field_binding_id": self.field_binding_id,
            "output_name": self.output_name,
            "parameter_id": self.parameter_id,
            "value": self.value,
            "value_type": self.value_type,
            "operator": self.operator,
            "target_type": self.target_type,
            "args": self.args,
            "cases": self.cases,
            "else_expr": self.else_expr,
        }
        for name, value in values.items():
            if name not in allowed and value not in (None, [], {}):
                raise ValueError(f"{self.kind} 表达式不允许字段 {name}")
        return self


class NamedExpression(StrictContract):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    expression: Expression


class OrderExpression(StrictContract):
    expression: Expression
    direction: Literal["asc", "desc"] = "asc"


class PlanNode(StrictContract):
    node_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    kind: NodeKind
    entity_id: str | None = None
    input: "PlanNode | None" = None
    left: "PlanNode | None" = None
    right: "PlanNode | None" = None
    join_type: Literal["inner", "left", "full"] | None = None
    on: Expression | None = None
    predicate: Expression | None = None
    projections: list[NamedExpression] = Field(default_factory=list)
    group_by: list[NamedExpression] = Field(default_factory=list)
    aggregates: list[NamedExpression] = Field(default_factory=list)
    inputs: list["PlanNode"] = Field(default_factory=list)
    order_by: list[OrderExpression] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "scan" and not self.entity_id:
            raise ValueError("scan 节点缺少 entity_id")
        if self.kind in {"filter", "project", "aggregate", "sort"} and self.input is None:
            raise ValueError(f"{self.kind} 节点缺少 input")
        if self.kind == "join":
            if self.left is None or self.right is None or self.on is None:
                raise ValueError("join 节点缺少 left/right/on")
            if self.join_type is None:
                raise ValueError("join 节点缺少 join_type")
        if self.kind == "filter" and self.predicate is None:
            raise ValueError("filter 节点缺少 predicate")
        if self.kind == "project" and not self.projections:
            raise ValueError("project 节点至少需要一个输出")
        if self.kind == "aggregate" and not self.aggregates:
            raise ValueError("aggregate 节点至少需要一个聚合输出")
        if self.kind == "union_all" and len(self.inputs) < 2:
            raise ValueError("union_all 节点至少需要两个输入")
        if self.kind == "sort" and not self.order_by:
            raise ValueError("sort 节点至少需要一个排序表达式")
        allowed = {
            "scan": {"entity_id"},
            "join": {"left", "right", "join_type", "on"},
            "filter": {"input", "predicate"},
            "project": {"input", "projections"},
            "aggregate": {"input", "group_by", "aggregates"},
            "union_all": {"inputs"},
            "sort": {"input", "order_by"},
        }[self.kind]
        values = {
            "entity_id": self.entity_id,
            "input": self.input,
            "left": self.left,
            "right": self.right,
            "join_type": self.join_type,
            "on": self.on,
            "predicate": self.predicate,
            "projections": self.projections,
            "group_by": self.group_by,
            "aggregates": self.aggregates,
            "inputs": self.inputs,
            "order_by": self.order_by,
        }
        for name, value in values.items():
            if name not in allowed and value not in (None, [], {}):
                raise ValueError(f"{self.kind} 节点不允许字段 {name}")
        return self


class ResultColumn(StrictContract):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    logical_type: Literal[
        "string", "integer", "decimal", "money", "date", "datetime", "boolean",
    ]
    nullable: bool = True


class RelationalPlan(StrictContract):
    version: Literal[3] = 3
    plan_id: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    root: PlanNode
    result_schema: list[ResultColumn] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_nodes(self):
        node_ids: set[str] = set()

        def visit(node: PlanNode):
            if node.node_id in node_ids:
                raise ValueError(f"关系计划节点 ID 重复: {node.node_id}")
            node_ids.add(node.node_id)
            for child in (node.input, node.left, node.right):
                if child is not None:
                    visit(child)
            for child in node.inputs:
                visit(child)

        visit(self.root)
        names = [item.name.casefold() for item in self.result_schema]
        if len(names) != len(set(names)):
            raise ValueError("关系计划结果列重复")
        return self


WhenThen.model_rebuild()
Expression.model_rebuild()
PlanNode.model_rebuild()
