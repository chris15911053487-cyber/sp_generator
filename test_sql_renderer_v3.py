import pytest

from app.contracts.relational_plan import Expression, NamedExpression, PlanNode, RelationalPlan, ResultColumn
from app.services.sql_renderer_v3 import SqlRenderError, SqlRendererV3
from v3_test_helpers import binding, contract, plan


def test_renderer_uses_real_identifiers_and_full_day_upper_bound():
    sql = SqlRendererV3(contract(), binding()).render_query(plan())
    assert "FROM [dbo].[OINV] AS [i]" in sql
    assert "[i].[DocEntry] AS [invoice_id]" in sql
    assert "DATEADD(DAY, 1, @ToDate)" in sql
    assert "< DATEADD" in sql
    assert "SP_RESULT" not in sql
    assert '"dbo"."OINV"' not in sql


def test_renderer_is_deterministic_and_procedure_wraps_same_query():
    renderer = SqlRendererV3(contract(), binding())
    first = renderer.render_query(plan())
    second = renderer.render_query(plan())
    procedure = renderer.render_procedure(plan())
    assert first == second
    assert "CREATE OR ALTER PROCEDURE [dbo].[usp_invoice_income]" in procedure
    assert "DATEADD(DAY, 1, @ToDate)" in procedure
    assert "[src].[invoice_amount] AS [Amount]" in procedure


def test_renderer_can_create_isolated_temporary_procedure():
    procedure = SqlRendererV3(
        contract(), binding()
    ).render_procedure(plan(), temporary_name="#v3_test")
    assert procedure.startswith("CREATE PROCEDURE [#v3_test]")
    assert "CREATE OR ALTER" not in procedure


def test_string_comparison_gets_explicit_database_collation():
    base = PlanNode(node_id="scan_invoice", kind="scan", entity_id="invoice")
    filtered = PlanNode(
        node_id="filter_customer",
        kind="filter",
        input=base,
        predicate=Expression(
            kind="binary",
            operator="=",
            args=[
                Expression(kind="column", field_binding_id="customer_code"),
                Expression(kind="literal", value="C001", value_type="string"),
            ],
        ),
    )
    projected = PlanNode(
        node_id="project_customer",
        kind="project",
        input=filtered,
        projections=[
            NamedExpression(
                name="CustomerCode",
                expression=Expression(
                    kind="column",
                    field_binding_id="customer_code",
                ),
            )
        ],
    )
    value = RelationalPlan(
        plan_id="customer_filter",
        purpose="客户过滤",
        root=projected,
        result_schema=[
            ResultColumn(name="CustomerCode", logical_type="string")
        ],
    )
    sql = SqlRendererV3(contract(), binding()).render_query(value)
    assert "COLLATE DATABASE_DEFAULT" in sql


def test_arbitrary_operator_is_rejected():
    value = plan()
    payload = value.model_dump()
    payload["root"]["input"]["predicate"]["operator"] = "DROP TABLE"
    poisoned = RelationalPlan.model_validate(payload)
    with pytest.raises(SqlRenderError) as error:
        SqlRendererV3(contract(), binding()).render_query(poisoned)
    assert error.value.code == "PLAN_OPERATOR_NOT_ALLOWED"


def test_cast_function_is_rejected_by_the_renderer_allowlist():
    value = plan()
    payload = value.model_dump()
    payload["root"]["projections"][1]["expression"] = {
        "kind": "function",
        "operator": "CAST",
        "args": [{
            "kind": "column",
            "field_binding_id": "invoice_amount",
        }],
    }
    poisoned = RelationalPlan.model_validate(payload)
    with pytest.raises(SqlRenderError) as error:
        SqlRendererV3(contract(), binding()).render_query(poisoned)
    assert error.value.code == "PLAN_FUNCTION_NOT_ALLOWED"


def test_controlled_cast_uses_a_fixed_sql_type():
    value = plan()
    payload = value.model_dump()
    payload["root"]["projections"][0]["expression"] = {
        "kind": "cast",
        "target_type": "string",
        "args": [{
            "kind": "column",
            "field_binding_id": "invoice_id",
        }],
    }
    casted = RelationalPlan.model_validate(payload)
    sql = SqlRendererV3(contract(), binding()).render_query(casted)
    assert "CAST([src].[invoice_id] AS nvarchar(4000))" in sql


def test_typed_calendar_bucket_functions_render_deterministically():
    value = plan()
    payload = value.model_dump()
    payload["root"]["projections"][0]["expression"] = {
        "kind": "function",
        "operator": "DATEFROMPARTS",
        "args": [
            {
                "kind": "function",
                "operator": "YEAR",
                "args": [{
                    "kind": "column",
                    "field_binding_id": "invoice_date",
                }],
            },
            {
                "kind": "function",
                "operator": "MONTH",
                "args": [{
                    "kind": "column",
                    "field_binding_id": "invoice_date",
                }],
            },
            {"kind": "literal", "value": 1, "value_type": "integer"},
        ],
    }
    bucketed = RelationalPlan.model_validate(payload)

    sql = SqlRendererV3(contract(), binding()).render_query(bucketed)

    assert (
        "DATEFROMPARTS(YEAR([src].[invoice_date]), "
        "MONTH([src].[invoice_date]), 1)"
    ) in sql
