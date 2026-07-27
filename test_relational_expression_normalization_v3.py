import json
from types import SimpleNamespace

from app.agent.nodes import (
    _canonicalize_nary_boolean_expressions,
    _generate_relational_plan_v3,
    _lower_sibling_output_dependencies_v3,
)
from app.contracts.relational_plan import Expression, RelationalPlan
from app.services.sql_renderer_v3 import SqlRendererV3
from v3_test_helpers import binding, contract, plan


def _literal(value):
    return {"kind": "literal", "value": value, "value_type": "boolean"}


def test_nary_and_is_folded_to_valid_binary_tree():
    raw = {
        "kind": "binary",
        "operator": "AND",
        "args": [_literal(True), _literal(False), _literal(True)],
    }

    normalized = _canonicalize_nary_boolean_expressions(raw)
    expression = Expression.model_validate(normalized)

    assert expression.operator == "AND"
    assert expression.args[0].operator == "AND"
    assert len(expression.args) == 2
    assert len(expression.args[0].args) == 2


def test_non_boolean_binary_is_not_silently_folded():
    raw = {
        "kind": "binary",
        "operator": "+",
        "args": [_literal(True), _literal(False), _literal(True)],
    }

    assert _canonicalize_nary_boolean_expressions(raw) == raw


def test_allowlisted_function_name_alias_is_normalized():
    raw = {
        "kind": "function",
        "value": "dateadd",
        "args": [
            {"kind": "literal", "value": "day", "value_type": "string"},
            {"kind": "literal", "value": 1, "value_type": "integer"},
            {"kind": "parameter", "parameter_id": "to_date"},
        ],
    }

    normalized = _canonicalize_nary_boolean_expressions(raw)
    expression = Expression.model_validate(normalized)

    assert expression.operator == "DATEADD"
    assert expression.value is None


def test_unknown_function_alias_is_not_normalized():
    raw = {
        "kind": "function",
        "value": "dangerous_function",
        "args": [{"kind": "literal", "value": 1, "value_type": "integer"}],
    }

    assert _canonicalize_nary_boolean_expressions(raw) == raw


def test_sibling_output_dependency_is_lowered_to_nested_projects():
    raw = {
        "version": 3,
        "plan_id": "amounts",
        "purpose": "派生未税金额",
        "root": {
            "node_id": "project_amounts",
            "kind": "project",
            "input": {
                "node_id": "scan_invoice",
                "kind": "scan",
                "entity_id": "invoice",
            },
            "projections": [
                {
                    "name": "GrossAmount",
                    "expression": {
                        "kind": "column",
                        "field_binding_id": "invoice_amount",
                    },
                },
                {
                    "name": "TaxAmount",
                    "expression": {
                        "kind": "column",
                        "field_binding_id": "invoice_amount",
                    },
                },
                {
                    "name": "NetAmount",
                    "expression": {
                        "kind": "binary",
                        "operator": "-",
                        "args": [
                            {"kind": "output", "output_name": "GrossAmount"},
                            {"kind": "output", "output_name": "TaxAmount"},
                        ],
                    },
                },
            ],
        },
        "result_schema": [
            {"name": "GrossAmount", "logical_type": "money"},
            {"name": "TaxAmount", "logical_type": "money"},
            {"name": "NetAmount", "logical_type": "money"},
        ],
    }

    lowered = _lower_sibling_output_dependencies_v3(raw)
    relational_plan = RelationalPlan.model_validate(lowered)
    sql = SqlRendererV3(contract(), binding()).render_query(relational_plan)

    assert lowered["root"]["input"]["kind"] == "project"
    assert lowered["root"]["projections"][0]["expression"] == {
        "kind": "output",
        "output_name": "GrossAmount",
    }
    assert "[src].[GrossAmount]" in sql


def test_single_entity_detail_plan_is_compiled_without_calling_llm():
    class NoLlm:
        def invoke(self, _messages):
            raise AssertionError("确定性合同不应调用 LLM 生成关系计划")

    generated = _generate_relational_plan_v3(
        NoLlm(),
        "procedure",
        contract(),
        binding(),
        plan().result_schema,
        [],
    )
    sql = SqlRendererV3(contract(), binding()).render_query(generated)

    assert "[dbo].[OINV]" in sql
    assert "DATEADD(DAY, 1, @ToDate)" in sql
    assert "[InvoiceId]" in sql


def test_plan_generation_repairs_expression_rejected_by_renderer():
    invalid = plan().model_dump(mode="json")
    invalid["root"]["projections"][1]["expression"] = {
        "kind": "function",
        "operator": "CAST",
        "args": [{
            "kind": "column",
            "field_binding_id": "invoice_amount",
        }],
    }
    valid = plan().model_dump(mode="json")

    class FakeLlm:
        def __init__(self):
            self.responses = [invalid, valid]

        def invoke(self, _messages):
            return SimpleNamespace(
                content=json.dumps(self.responses.pop(0))
            )

    events = []
    generated = _generate_relational_plan_v3(
        FakeLlm(),
        "reference fact amount",
        contract(),
        binding(),
        plan().result_schema,
        events,
        allow_deterministic=False,
    )

    assert generated == plan()
    assert events[0]["status"] == "repaired"
    assert "CAST" in events[0]["error"]


def test_plan_generation_repairs_database_result_contract_failure():
    payload = plan().model_dump(mode="json")

    class FakeLlm:
        def __init__(self):
            self.calls = 0

        def invoke(self, _messages):
            self.calls += 1
            return SimpleNamespace(content=json.dumps(payload))

    validations = 0

    def post_validator(_plan):
        nonlocal validations
        validations += 1
        if validations == 1:
            error = ValueError("实际 datetime，期望 date")
            error.evidence = {
                "errors": [{"kind": "type", "actual": "datetime"}]
            }
            raise error

    events = []
    generated = _generate_relational_plan_v3(
        FakeLlm(),
        "reference fact amount",
        contract(),
        binding(),
        plan().result_schema,
        events,
        post_validator=post_validator,
        allow_deterministic=False,
    )

    assert generated == plan()
    assert validations == 2
    assert events[0]["status"] == "repaired"
    assert events[0]["evidence"]["errors"][0]["actual"] == "datetime"
