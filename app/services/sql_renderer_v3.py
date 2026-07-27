"""把受限关系计划确定性渲染为 SQL Server SQL。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from app.contracts.relational_plan import Expression, PlanNode, RelationalPlan
from app.contracts.schema import FieldBinding, SchemaBinding
from app.contracts.semantic import SemanticContract, SemanticParameter


RENDERER_VERSION = "sqlserver-relational-v3.1"

_BINARY_OPERATORS = {
    "=",
    "<>",
    ">",
    ">=",
    "<",
    "<=",
    "AND",
    "OR",
    "+",
    "-",
    "*",
    "/",
    "LIKE",
}
_UNARY_OPERATORS = {"NOT", "IS NULL", "IS NOT NULL", "NEGATE"}
_FUNCTIONS = {
    "ABS",
    "AVG",
    "COALESCE",
    "CONCAT",
    "COUNT",
    "COUNT_DISTINCT",
    "DATEADD",
    "DATEDIFF",
    "LOWER",
    "LTRIM",
    "MAX",
    "MIN",
    "NULLIF",
    "RTRIM",
    "SUM",
    "UPPER",
    "YEAR",
    "MONTH",
    "DATEFROMPARTS",
    "EOMONTH",
}
_CAST_TYPES = {
    "string": "nvarchar(4000)",
    "integer": "bigint",
    "decimal": "decimal(38, 10)",
    "money": "decimal(19, 4)",
    "date": "date",
    "datetime": "datetime2(7)",
    "boolean": "bit",
}


class SqlRenderError(ValueError):
    def __init__(self, code: str, message: str, *, plan_path: str = ""):
        super().__init__(message)
        self.code = code
        self.plan_path = plan_path


@dataclass(frozen=True)
class _CompiledNode:
    sql: str
    columns: tuple[str, ...]
    field_columns: frozenset[str]


def quote_identifier(value: str) -> str:
    if not value or "\x00" in value:
        raise SqlRenderError("PLAN_IDENTIFIER_INVALID", "SQL 标识符不能为空")
    return "[" + value.replace("]", "]]") + "]"


def _sql_type(parameter: SemanticParameter) -> str:
    return {
        "string": "nvarchar(4000)",
        "integer": "bigint",
        "decimal": "decimal(38, 10)",
        "money": "decimal(19, 4)",
        "date": "date",
        "datetime": "datetime2(7)",
        "boolean": "bit",
    }[parameter.logical_type]


def _literal(expression: Expression) -> str:
    value_type = expression.value_type
    value = expression.value
    if value_type == "null":
        if value is not None:
            raise SqlRenderError(
                "PLAN_LITERAL_INVALID",
                "null 字面量的 value 必须为 null",
            )
        return "NULL"
    if value is None:
        raise SqlRenderError("PLAN_LITERAL_INVALID", "非 null 字面量缺少 value")
    if value_type == "string":
        return "N'" + str(value).replace("'", "''") + "'"
    if value_type == "boolean":
        if not isinstance(value, bool):
            raise SqlRenderError("PLAN_LITERAL_INVALID", "boolean 字面量必须是布尔值")
        return "1" if value else "0"
    if value_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SqlRenderError("PLAN_LITERAL_INVALID", "integer 字面量必须是整数")
        return str(value)
    if value_type == "decimal":
        try:
            return format(Decimal(str(value)), "f")
        except Exception as exc:
            raise SqlRenderError(
                "PLAN_LITERAL_INVALID",
                "decimal 字面量无效",
            ) from exc
    if value_type == "date":
        try:
            normalized = date.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise SqlRenderError("PLAN_LITERAL_INVALID", "date 字面量无效") from exc
        return f"CONVERT(date, N'{normalized}', 23)"
    if value_type == "datetime":
        try:
            normalized = datetime.fromisoformat(str(value)).isoformat()
        except ValueError as exc:
            raise SqlRenderError(
                "PLAN_LITERAL_INVALID",
                "datetime 字面量无效",
            ) from exc
        escaped = normalized.replace("'", "''")
        return f"CONVERT(datetime2(7), N'{escaped}', 126)"
    raise SqlRenderError("PLAN_LITERAL_INVALID", f"不支持字面量类型 {value_type}")


def _parameter_default(parameter: SemanticParameter) -> str:
    if parameter.default is None:
        return "NULL"
    if parameter.logical_type in {"date", "datetime"}:
        return "'" + str(parameter.default).replace("'", "''") + "'"
    expression = Expression(
        kind="literal",
        value=parameter.default,
        value_type={
            "string": "string",
            "integer": "integer",
            "decimal": "decimal",
            "money": "decimal",
            "boolean": "boolean",
        }[parameter.logical_type],
    )
    return _literal(expression)


class SqlRendererV3:
    def __init__(
        self,
        contract: SemanticContract,
        binding: SchemaBinding,
    ):
        if binding.contract_hash != contract.content_hash:
            raise SqlRenderError(
                "PLAN_CONTRACT_HASH_MISMATCH",
                "SchemaBinding 与 SemanticContract 不属于同一版本",
            )
        self.contract = contract
        self.binding = binding
        self.parameters = {item.id: item for item in contract.parameters}
        self.fields = {item.binding_id: item for item in binding.fields}
        self.entities = {item.entity_id: item for item in binding.entities}

    def render_query(self, plan: RelationalPlan) -> str:
        compiled = self._compile_node(plan.root, "root")
        actual = [item.casefold() for item in compiled.columns]
        expected = [item.name.casefold() for item in plan.result_schema]
        if actual != expected:
            raise SqlRenderError(
                "PLAN_RESULT_SCHEMA_MISMATCH",
                "关系计划输出列与 result_schema 不一致",
                plan_path="root",
            )
        return compiled.sql.rstrip() + ";"

    def render_procedure(
        self,
        plan: RelationalPlan,
        schema: str = "dbo",
        temporary_name: str | None = None,
    ) -> str:
        query = self.render_query(plan)
        declarations = []
        for parameter in self.contract.parameters:
            default = ""
            if not parameter.required:
                default = " = " + _parameter_default(parameter)
            declarations.append(
                f"    {parameter.name} {_sql_type(parameter)}{default}"
            )
        parameter_sql = ""
        if declarations:
            parameter_sql = "\n" + ",\n".join(declarations)
        procedure_identity = (
            quote_identifier(temporary_name)
            if temporary_name is not None
            else f"{quote_identifier(schema)}."
            f"{quote_identifier(self.contract.procedure_name)}"
        )
        create_keyword = (
            "CREATE PROCEDURE"
            if temporary_name is not None else "CREATE OR ALTER PROCEDURE"
        )
        return (
            f"{create_keyword} "
            f"{procedure_identity}"
            f"{parameter_sql}\n"
            "AS\n"
            "BEGIN\n"
            "    SET NOCOUNT ON;\n\n"
            + "\n".join("    " + line for line in query.splitlines())
            + "\nEND;"
        )

    def _expression(
        self,
        expression: Expression,
        field_scope: dict[str, str],
        output_scope: dict[str, str],
        path: str,
    ) -> str:
        if expression.kind == "column":
            reference = field_scope.get(str(expression.field_binding_id))
            if reference is None:
                raise SqlRenderError(
                    "PLAN_FIELD_OUT_OF_SCOPE",
                    f"字段 {expression.field_binding_id} 不在当前节点作用域",
                    plan_path=path,
                )
            return reference
        if expression.kind == "output":
            reference = output_scope.get(str(expression.output_name).casefold())
            if reference is None:
                raise SqlRenderError(
                    "PLAN_OUTPUT_OUT_OF_SCOPE",
                    f"输出 {expression.output_name} 不在当前节点作用域",
                    plan_path=path,
                )
            return reference
        if expression.kind == "parameter":
            parameter = self.parameters.get(str(expression.parameter_id))
            if parameter is None:
                raise SqlRenderError(
                    "PLAN_PARAMETER_UNKNOWN",
                    f"参数 {expression.parameter_id} 未在语义合同中声明",
                    plan_path=path,
                )
            return parameter.name
        if expression.kind == "literal":
            return _literal(expression)
        if expression.kind == "binary":
            operator = str(expression.operator).upper()
            if operator not in _BINARY_OPERATORS:
                raise SqlRenderError(
                    "PLAN_OPERATOR_NOT_ALLOWED",
                    f"不允许二元运算符 {operator}",
                    plan_path=path,
                )
            left = self._expression(
                expression.args[0], field_scope, output_scope, path + ".args[0]"
            )
            right = self._expression(
                expression.args[1], field_scope, output_scope, path + ".args[1]"
            )
            return f"({left} {operator} {right})"
        if expression.kind == "unary":
            operator = str(expression.operator).upper()
            if operator not in _UNARY_OPERATORS:
                raise SqlRenderError(
                    "PLAN_OPERATOR_NOT_ALLOWED",
                    f"不允许一元运算符 {operator}",
                    plan_path=path,
                )
            argument = self._expression(
                expression.args[0], field_scope, output_scope, path + ".args[0]"
            )
            if operator == "NEGATE":
                return f"(-({argument}))"
            if operator.startswith("IS "):
                return f"({argument} {operator})"
            return f"({operator} ({argument}))"
        if expression.kind == "function":
            function = str(expression.operator).upper()
            if function not in _FUNCTIONS:
                raise SqlRenderError(
                    "PLAN_FUNCTION_NOT_ALLOWED",
                    f"不允许函数 {function}",
                    plan_path=path,
                )
            rendered_arguments = []
            for index, item in enumerate(expression.args):
                if function in {"DATEADD", "DATEDIFF"} and index == 0:
                    datepart = (
                        str(item.value).upper()
                        if item.kind == "literal" and item.value_type == "string"
                        else ""
                    )
                    if datepart not in {
                        "YEAR", "QUARTER", "MONTH", "DAY", "HOUR", "MINUTE",
                        "SECOND", "MILLISECOND",
                    }:
                        raise SqlRenderError(
                            "PLAN_DATEPART_INVALID",
                            f"{function} 的 datepart 不受支持",
                            plan_path=f"{path}.args[0]",
                        )
                    rendered_arguments.append(datepart)
                    continue
                rendered_arguments.append(
                    self._expression(
                        item,
                        field_scope,
                        output_scope,
                        f"{path}.args[{index}]",
                    )
                )
            if function == "COUNT_DISTINCT":
                if len(rendered_arguments) != 1:
                    raise SqlRenderError(
                        "PLAN_FUNCTION_ARGUMENT_INVALID",
                        "COUNT_DISTINCT 必须且只能包含一个参数",
                        plan_path=path,
                    )
                return f"COUNT(DISTINCT {rendered_arguments[0]})"
            arguments = ", ".join(rendered_arguments)
            return f"{function}({arguments})"
        if expression.kind == "cast":
            argument = self._expression(
                expression.args[0],
                field_scope,
                output_scope,
                path + ".args[0]",
            )
            sql_type = _CAST_TYPES[str(expression.target_type)]
            return f"CAST({argument} AS {sql_type})"
        if expression.kind == "case":
            parts = ["CASE"]
            for index, item in enumerate(expression.cases):
                when = self._expression(
                    item.when,
                    field_scope,
                    output_scope,
                    f"{path}.cases[{index}].when",
                )
                then = self._expression(
                    item.then,
                    field_scope,
                    output_scope,
                    f"{path}.cases[{index}].then",
                )
                parts.append(f"WHEN {when} THEN {then}")
            if expression.else_expr is not None:
                parts.append(
                    "ELSE "
                    + self._expression(
                        expression.else_expr,
                        field_scope,
                        output_scope,
                        path + ".else",
                    )
                )
            parts.append("END")
            return "(" + " ".join(parts) + ")"
        raise SqlRenderError(
            "PLAN_EXPRESSION_KIND_UNKNOWN",
            f"不支持表达式类型 {expression.kind}",
            plan_path=path,
        )

    @staticmethod
    def _scope(
        compiled: _CompiledNode,
        alias: str,
    ) -> tuple[dict[str, str], dict[str, str]]:
        field_scope = {
            name: f"{quote_identifier(alias)}.{quote_identifier(name)}"
            for name in compiled.field_columns
        }
        output_scope = {
            name.casefold(): f"{quote_identifier(alias)}.{quote_identifier(name)}"
            for name in compiled.columns
        }
        return field_scope, output_scope

    @staticmethod
    def _select_columns(columns: Iterable[str], alias: str) -> str:
        return ",\n".join(
            f"    {quote_identifier(alias)}.{quote_identifier(name)} "
            f"AS {quote_identifier(name)}"
            for name in columns
        )

    def _compile_node(self, node: PlanNode, path: str) -> _CompiledNode:
        if node.kind == "scan":
            entity = self.entities.get(str(node.entity_id))
            if entity is None:
                raise SqlRenderError(
                    "PLAN_ENTITY_UNKNOWN",
                    f"实体 {node.entity_id} 未绑定",
                    plan_path=path,
                )
            fields = [
                item for item in self.binding.fields
                if item.entity_id == entity.entity_id
            ]
            if not fields:
                raise SqlRenderError(
                    "PLAN_ENTITY_HAS_NO_FIELDS",
                    f"实体 {node.entity_id} 没有字段绑定",
                    plan_path=path,
                )
            source_alias = entity.alias
            projection_lines = []
            for item in fields:
                source = (
                    f"{quote_identifier(source_alias)}."
                    f"{quote_identifier(item.column)}"
                )
                if item.collation:
                    source = f"({source} COLLATE DATABASE_DEFAULT)"
                projection_lines.append(
                    f"    {source} AS {quote_identifier(item.binding_id)}"
                )
            projections = ",\n".join(projection_lines)
            sql = (
                "SELECT\n"
                f"{projections}\n"
                f"FROM {quote_identifier(entity.schema)}."
                f"{quote_identifier(entity.object_name)} "
                f"AS {quote_identifier(source_alias)}"
            )
            names = tuple(item.binding_id for item in fields)
            return _CompiledNode(sql, names, frozenset(names))

        if node.kind == "join":
            left = self._compile_node(node.left, path + ".left")
            right = self._compile_node(node.right, path + ".right")
            duplicates = set(left.columns) & set(right.columns)
            if duplicates:
                raise SqlRenderError(
                    "PLAN_JOIN_COLUMN_DUPLICATE",
                    "关联输入含重复内部列: " + ", ".join(sorted(duplicates)),
                    plan_path=path,
                )
            left_fields, left_outputs = self._scope(left, "l")
            right_fields, right_outputs = self._scope(right, "r")
            predicate = self._expression(
                node.on,
                {**left_fields, **right_fields},
                {**left_outputs, **right_outputs},
                path + ".on",
            )
            columns = left.columns + right.columns
            selects = self._select_columns(left.columns, "l")
            if right.columns:
                selects += ",\n" + self._select_columns(right.columns, "r")
            join_type = {
                "inner": "INNER JOIN",
                "left": "LEFT JOIN",
                "full": "FULL OUTER JOIN",
            }[str(node.join_type)]
            sql = (
                "SELECT\n"
                f"{selects}\n"
                f"FROM (\n{_indent(left.sql)}\n) AS [l]\n"
                f"{join_type} (\n{_indent(right.sql)}\n) AS [r]\n"
                f"    ON {predicate}"
            )
            return _CompiledNode(
                sql,
                columns,
                left.field_columns | right.field_columns,
            )

        if node.kind == "union_all":
            compiled_inputs = [
                self._compile_node(item, f"{path}.inputs[{index}]")
                for index, item in enumerate(node.inputs)
            ]
            expected = tuple(name.casefold() for name in compiled_inputs[0].columns)
            for item in compiled_inputs[1:]:
                if tuple(name.casefold() for name in item.columns) != expected:
                    raise SqlRenderError(
                        "PLAN_UNION_SCHEMA_MISMATCH",
                        "UNION ALL 的各输入列必须完全一致",
                        plan_path=path,
                    )
            branches = []
            for index, item in enumerate(compiled_inputs, start=1):
                alias = f"u{index}"
                branches.append(
                    "SELECT\n"
                    f"{self._select_columns(item.columns, alias)}\n"
                    "FROM (\n"
                    f"{_indent(item.sql)}\n"
                    f") AS {quote_identifier(alias)}"
                )
            sql = "\nUNION ALL\n".join(branches)
            return _CompiledNode(
                sql,
                compiled_inputs[0].columns,
                frozenset.intersection(
                    *(item.field_columns for item in compiled_inputs)
                ),
            )

        child = self._compile_node(node.input, path + ".input")
        field_scope, output_scope = self._scope(child, "src")
        source = f"(\n{_indent(child.sql)}\n) AS [src]"

        if node.kind == "filter":
            predicate = self._expression(
                node.predicate,
                field_scope,
                output_scope,
                path + ".predicate",
            )
            return _CompiledNode(
                "SELECT\n"
                f"{self._select_columns(child.columns, 'src')}\n"
                f"FROM {source}\n"
                f"WHERE {predicate}",
                child.columns,
                child.field_columns,
            )

        if node.kind == "project":
            names = tuple(item.name for item in node.projections)
            _ensure_unique(names, path)
            projections = ",\n".join(
                "    "
                + self._expression(
                    item.expression,
                    field_scope,
                    output_scope,
                    f"{path}.projections[{index}]",
                )
                + f" AS {quote_identifier(item.name)}"
                for index, item in enumerate(node.projections)
            )
            return _CompiledNode(
                f"SELECT\n{projections}\nFROM {source}",
                names,
                frozenset(),
            )

        if node.kind == "aggregate":
            named = node.group_by + node.aggregates
            names = tuple(item.name for item in named)
            _ensure_unique(names, path)
            projections = ",\n".join(
                "    "
                + self._expression(
                    item.expression,
                    field_scope,
                    output_scope,
                    f"{path}.outputs[{index}]",
                )
                + f" AS {quote_identifier(item.name)}"
                for index, item in enumerate(named)
            )
            sql = f"SELECT\n{projections}\nFROM {source}"
            if node.group_by:
                group_sql = ", ".join(
                    self._expression(
                        item.expression,
                        field_scope,
                        output_scope,
                        f"{path}.group_by[{index}]",
                    )
                    for index, item in enumerate(node.group_by)
                )
                sql += "\nGROUP BY " + group_sql
            return _CompiledNode(sql, names, frozenset())

        if node.kind == "sort":
            order = ", ".join(
                self._expression(
                    item.expression,
                    field_scope,
                    output_scope,
                    f"{path}.order_by[{index}]",
                )
                + " "
                + item.direction.upper()
                for index, item in enumerate(node.order_by)
            )
            return _CompiledNode(
                "SELECT\n"
                f"{self._select_columns(child.columns, 'src')}\n"
                f"FROM {source}\n"
                f"ORDER BY {order}",
                child.columns,
                child.field_columns,
            )

        raise SqlRenderError(
            "PLAN_NODE_KIND_UNKNOWN",
            f"不支持计划节点 {node.kind}",
            plan_path=path,
        )


def _ensure_unique(names: tuple[str, ...], path: str) -> None:
    normalized = [item.casefold() for item in names]
    if len(normalized) != len(set(normalized)):
        raise SqlRenderError(
            "PLAN_OUTPUT_DUPLICATE",
            "节点输出名称不能重复",
            plan_path=path,
        )


def _indent(sql: str) -> str:
    return "\n".join("    " + line for line in sql.splitlines())
