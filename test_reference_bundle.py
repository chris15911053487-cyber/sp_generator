import pytest

from app.contracts.reference import (
    ComparatorSpec,
    ReferenceFactDesign,
    ValidationCase,
)
from app.contracts.relational_plan import (
    Expression,
    NamedExpression,
    PlanNode,
    RelationalPlan,
    ResultColumn,
)
from app.contracts.semantic import SemanticContract
from app.services.reference_planner import (
    ReferenceBuildError,
    ReferenceFactDraft,
    freeze_reference_bundle,
)
from v3_test_helpers import binding, contract, plan


def test_reference_fact_cannot_copy_monolithic_final_result():
    with pytest.raises(ValueError, match="独立业务事实"):
        ReferenceFactDesign(
            fact_id="final_result",
            meaning="复制整个 SP",
            actual_projection=["InvoiceId", "Amount"],
        )


def test_reference_must_compile_then_preflight_before_freeze():
    calls = []

    def compiler(artifact, name, sql, parameters):
        calls.append(("compile", artifact, sql))
        return {
            "ok": True,
            "result_columns": [
                {"name": "InvoiceId", "sql_type": "int", "nullable": False},
                {"name": "Amount", "sql_type": "decimal(19,6)", "nullable": False},
            ],
        }

    def preflight(sql, parameters):
        calls.append(("preflight", parameters))
        return [{"InvoiceId": 1, "Amount": 100.0}]

    semantic = contract()
    schema = binding(semantic)
    result = freeze_reference_bundle(
        semantic,
        schema,
        [
            ReferenceFactDraft(
                fact_id="invoice_income",
                meaning="应收收入",
                actual_projection=["InvoiceId", "Amount"],
                plan=plan(),
                comparator=ComparatorSpec(
                    type="keyed_rows_equal",
                    key_columns=["InvoiceId"],
                    compare_columns=["Amount"],
                ),
            )
        ],
        [
            ValidationCase(
                case_id="coverage",
                kind="coverage",
                parameters={
                    "from_date": "2026-01-01",
                    "to_date": "2026-01-31",
                },
            )
        ],
        compiler=compiler,
        preflight_executor=preflight,
    )
    assert result.status == "reference_ready"
    assert [item[0] for item in calls] == ["compile", "preflight"]
    assert result.facts[0].allowed_object_ids == [101]


def test_empty_reference_cannot_be_frozen_as_success():
    semantic = contract()
    with pytest.raises(ReferenceBuildError) as error:
        freeze_reference_bundle(
            semantic,
            binding(semantic),
            [
                ReferenceFactDraft(
                    fact_id="invoice_income",
                    meaning="应收收入",
                    actual_projection=["InvoiceId", "Amount"],
                    plan=plan(),
                    comparator=ComparatorSpec(
                        type="keyed_rows_equal",
                        key_columns=["InvoiceId"],
                        compare_columns=["Amount"],
                    ),
                )
            ],
            [
                ValidationCase(
                    case_id="coverage",
                    kind="coverage",
                    parameters={
                        "from_date": "2026-01-01",
                        "to_date": "2026-01-31",
                    },
                )
            ],
            compiler=lambda *args: {"ok": True},
            preflight_executor=lambda *_: [],
        )
    assert error.value.code == "REFERENCE_COVERAGE_EMPTY"


def test_scalar_aggregate_requires_nonempty_source_coverage():
    payload = contract().model_dump()
    payload["result_mode"] = "scalar_summary"
    payload["grain"] = []
    payload["outputs"] = [
        {
            "id": "amount",
            "name": "TotalAmount",
            "meaning": "收入总额",
            "logical_type": "money",
            "nullable": True,
        }
    ]
    payload["source_fields"] = [
        {
            "id": "amount",
            "entity_id": "invoice",
            "meaning": "发票收入金额",
            "logical_type": "money",
        },
        {
            "id": "invoice_date",
            "entity_id": "invoice",
            "meaning": "发票业务日期",
            "logical_type": "date",
        },
    ]
    payload["facts"] = [{
        "id": "income_fact",
        "meaning": "按条件汇总发票收入",
        "entity_ids": ["invoice"],
        "dimensions": [],
        "measures": [{
            "id": "total_amount",
            "field_id": "amount",
            "meaning": "收入金额合计",
            "aggregation": "sum",
            "logical_type": "money",
        }],
        "filter_ids": ["invoice_date_range"],
        "grain": [],
    }]
    payload["result_bindings"] = [{
        "output_id": "amount",
        "expression": {
            "kind": "fact_value",
            "fact_value": {
                "fact_id": "income_fact",
                "value_id": "total_amount",
            },
        },
    }]
    semantic = SemanticContract.model_validate(payload)
    schema_binding = binding(semantic)
    filtered = plan().root.input
    aggregate = RelationalPlan(
        plan_id="aggregate_income",
        purpose="汇总收入",
        root=PlanNode(
            node_id="aggregate_total",
            kind="aggregate",
            input=filtered,
            aggregates=[
                NamedExpression(
                    name="TotalAmount",
                    expression=Expression(
                        kind="function",
                        operator="SUM",
                        args=[
                            Expression(
                                kind="column",
                                field_binding_id="invoice_amount",
                            )
                        ],
                    ),
                )
            ],
        ),
        result_schema=[
            ResultColumn(
                name="TotalAmount",
                logical_type="money",
                nullable=True,
            )
        ],
    )
    with pytest.raises(ReferenceBuildError) as error:
        freeze_reference_bundle(
            semantic,
            schema_binding,
            [
                ReferenceFactDraft(
                    fact_id="total_income",
                    meaning="收入总额",
                    actual_projection=["TotalAmount"],
                    plan=aggregate,
                    comparator=ComparatorSpec(
                        type="scalar_metrics_equal",
                        compare_columns=["TotalAmount"],
                    ),
                )
            ],
            [
                ValidationCase(
                    case_id="coverage",
                    kind="coverage",
                    parameters={
                        "from_date": "2026-01-01",
                        "to_date": "2026-01-31",
                    },
                )
            ],
            compiler=lambda *args: {"ok": True},
            preflight_executor=lambda sql, _params: (
                [{"CoverageCount": 0}]
                if "CoverageCount" in sql
                else [{"TotalAmount": None}]
            ),
        )
    assert error.value.code == "REFERENCE_COVERAGE_EMPTY"
