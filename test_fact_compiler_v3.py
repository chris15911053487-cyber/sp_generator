from decimal import Decimal
from datetime import datetime, timezone

import pytest

from app.contracts.reference import ComparatorSpec, ReferenceBundle, ReferenceFact, ValidationCase
from app.contracts.schema import (
    CatalogColumn,
    CatalogObject,
    CatalogSnapshot,
    EntityBinding,
    FieldBinding,
    SchemaBinding,
)
from app.contracts.semantic import SemanticContract
from app.services.catalog_v3 import catalog_fingerprint
from app.services.fact_compiler_v3 import (
    compile_contract_plan,
    compile_fact_plan,
    compose_expected_rows,
)
from app.services.plan_semantics_v3 import _filter_matches
from app.services.procedure_generator_v3 import generate_procedure_candidate
from app.services.reference_planner import coverage_plan
from app.services.reference_planner import (
    ReferenceFactDraft,
    freeze_reference_bundle,
)
from app.services.sql_renderer_v3 import SqlRendererV3
from app.services.validation_runner_v3 import (
    RuntimeResult,
    SnapshotExecution,
    _runtime_value_v3,
    validate_candidate_v3,
)
from test_semantic_facts_v3 import reconciliation_payload


def reconciliation_contract_and_binding():
    payload = reconciliation_payload()
    contract = SemanticContract.model_validate(payload)
    binding = SchemaBinding(
        contract_hash=contract.content_hash,
        catalog_fingerprint="a" * 64,
        entities=[
            EntityBinding(
                entity_id="sales",
                database="TEST_DB",
                schema="dbo",
                object="SalesIncome",
                object_id=101,
                alias="s",
            ),
            EntityBinding(
                entity_id="journal",
                database="TEST_DB",
                schema="dbo",
                object="JournalIncome",
                object_id=102,
                alias="j",
            ),
        ],
        fields=[
            FieldBinding(
                binding_id="sales_period_binding",
                semantic_id="sales_period",
                entity_id="sales",
                column="PeriodCode",
                column_id=1,
                sql_type="nvarchar",
                nullable=False,
                collation=None,
            ),
            FieldBinding(
                binding_id="sales_amount_binding",
                semantic_id="sales_amount",
                entity_id="sales",
                column="Amount",
                column_id=2,
                sql_type="decimal",
                nullable=False,
                collation=None,
            ),
            FieldBinding(
                binding_id="journal_period_binding",
                semantic_id="journal_period",
                entity_id="journal",
                column="PeriodCode",
                column_id=1,
                sql_type="nvarchar",
                nullable=False,
                collation=None,
            ),
            FieldBinding(
                binding_id="journal_amount_binding",
                semantic_id="journal_amount",
                entity_id="journal",
                column="Amount",
                column_id=2,
                sql_type="decimal",
                nullable=False,
                collation=None,
            ),
        ],
    )
    return contract, binding


def test_multi_source_contract_compiles_from_frozen_facts():
    contract, binding = reconciliation_contract_and_binding()
    plans = [
        compile_fact_plan(contract, binding, fact)
        for fact in contract.facts
    ]
    final_plan = compile_contract_plan(contract, binding)
    renderer = SqlRendererV3(contract, binding)

    fact_sql = [renderer.render_query(plan) for plan in plans]
    final_sql = renderer.render_query(final_plan)

    assert all("SUM(" in sql for sql in fact_sql)
    assert "FULL OUTER JOIN" in final_sql
    assert [column.name for column in final_plan.result_schema] == [
        "Period", "SalesRevenue", "JournalRevenue", "Difference",
    ]


def test_optional_null_parameter_compiles_to_explicit_filter_bypass():
    original_contract, original_binding = reconciliation_contract_and_binding()
    payload = original_contract.model_dump(mode="json")
    payload["parameters"] = [{
        "id": "customer_code",
        "name": "@CustomerCode",
        "logical_type": "string",
        "required": False,
        "default": None,
        "meaning": "可选客户编码",
        "boundary": "none",
    }]
    payload["filters"] = [{
        "id": "customer_filter",
        "meaning": "传入客户编码时筛选，否则返回全部客户",
        "field_ids": ["journal_period"],
        "parameter_ids": ["customer_code"],
        "operator": "eq",
        "literal_values": [],
        "skip_when_parameter_null": True,
    }]
    payload["facts"][1]["filter_ids"] = ["customer_filter"]
    semantic = SemanticContract.model_validate(payload)
    frozen = original_binding.model_copy(
        update={"contract_hash": semantic.content_hash}
    )

    plan = compile_fact_plan(semantic, frozen, semantic.facts[1])
    sql = SqlRendererV3(semantic, frozen).render_query(plan)
    assert _filter_matches(
        semantic.filters[0],
        plan.root.input.predicate,
        {
            item.binding_id: item.semantic_id
            for item in frozen.fields
        },
        {},
    )

    assert "@CustomerCode IS NULL" in sql
    assert "OR" in sql
    assert "[src].[journal_period_binding] = @CustomerCode" in sql


def test_date_dimension_is_cast_before_reference_schema_validation():
    original_contract, original_binding = reconciliation_contract_and_binding()
    contract_payload = original_contract.model_dump(mode="json")
    next(
        item for item in contract_payload["source_fields"]
        if item["id"] == "sales_period"
    )["logical_type"] = "date"
    next(
        item for item in contract_payload["outputs"]
        if item["id"] == "period"
    )["logical_type"] = "date"
    semantic = SemanticContract.model_validate(contract_payload)
    binding_payload = original_binding.model_dump(mode="json", by_alias=True)
    binding_payload["contract_hash"] = semantic.content_hash
    next(
        item for item in binding_payload["fields"]
        if item["semantic_id"] == "sales_period"
    )["sql_type"] = "datetime"
    frozen = SchemaBinding.model_validate(binding_payload)

    plan = compile_fact_plan(semantic, frozen, semantic.facts[0])
    sql = SqlRendererV3(semantic, frozen).render_query(plan)

    assert "CAST(" in sql
    assert " AS date)" in sql


def test_derived_period_dimension_is_explicit_and_compiles_to_string():
    original_contract, original_binding = reconciliation_contract_and_binding()
    payload = original_contract.model_dump(mode="json")
    for source_id in ("sales_period", "journal_period"):
        next(
            item for item in payload["source_fields"]
            if item["id"] == source_id
        )["logical_type"] = "date"
    for fact in payload["facts"]:
        source_id = fact["dimensions"][0].pop("field_id")
        fact["dimensions"][0].update({
            "logical_type": "string",
            "expression": {
                "kind": "function",
                "operator": "CONCAT",
                "args": [
                    {
                        "kind": "function",
                        "operator": "YEAR",
                        "args": [{"kind": "field", "field_id": source_id}],
                    },
                    {"kind": "literal", "value": "-"},
                    {
                        "kind": "function",
                        "operator": "MONTH",
                        "args": [{"kind": "field", "field_id": source_id}],
                    },
                ],
            },
        })
    semantic = SemanticContract.model_validate(payload)
    binding_payload = original_binding.model_dump(mode="json", by_alias=True)
    binding_payload["contract_hash"] = semantic.content_hash
    frozen = SchemaBinding.model_validate(binding_payload)

    plan = compile_fact_plan(semantic, frozen, semantic.facts[0])
    sql = SqlRendererV3(semantic, frozen).render_query(plan)

    assert "CONCAT(" in sql
    assert "YEAR(" in sql
    assert "MONTH(" in sql
    assert plan.result_schema[0].logical_type == "string"
    assert plan.result_schema[0].nullable is True


def test_coverage_count_schema_matches_sql_server_nullable_metadata():
    contract, binding = reconciliation_contract_and_binding()
    plan = compile_fact_plan(contract, binding, contract.facts[0])

    coverage = coverage_plan(plan, binding)

    assert coverage is not None
    assert coverage.result_schema[0].name == "CoverageCount"
    assert coverage.result_schema[0].nullable is True


def test_validation_runtime_preserves_decimal_for_expected_composition():
    value = Decimal("10.2500")

    assert _runtime_value_v3(value) is value


def test_single_source_aggregate_uses_the_same_frozen_fact_model():
    fact_ref = {
        "kind": "fact_value",
        "fact_value": {"fact_id": "sales_fact", "value_id": "revenue"},
    }
    payload = {
        "contract_id": "sales_total",
        "procedure_name": "usp_SalesTotal",
        "purpose": "汇总销售收入",
        "result_mode": "scalar_summary",
        "entities": [{"id": "sales", "meaning": "销售收入事实"}],
        "source_fields": [{
            "id": "sales_amount",
            "entity_id": "sales",
            "meaning": "销售收入系统币金额",
            "logical_type": "money",
            "nullable": False,
        }],
        "outputs": [{
            "id": "total_revenue",
            "name": "TotalRevenue",
            "meaning": "销售收入系统币合计",
            "logical_type": "money",
        }],
        "facts": [{
            "id": "sales_fact",
            "meaning": "销售收入汇总事实",
            "entity_ids": ["sales"],
            "measures": [{
                "id": "revenue",
                "field_id": "sales_amount",
                "meaning": "销售收入合计",
                "aggregation": "sum",
                "logical_type": "money",
            }],
        }],
        "result_bindings": [{
            "output_id": "total_revenue",
            "expression": fact_ref,
        }],
    }
    contract = SemanticContract.model_validate(payload)
    binding = SchemaBinding(
        contract_hash=contract.content_hash,
        catalog_fingerprint="b" * 64,
        entities=[EntityBinding(
            entity_id="sales",
            database="TEST_DB",
            schema="dbo",
            object="SalesIncome",
            object_id=101,
            alias="s",
        )],
        fields=[FieldBinding(
            binding_id="sales_amount_binding",
            semantic_id="sales_amount",
            entity_id="sales",
            column="Amount",
            column_id=2,
            sql_type="decimal",
            nullable=False,
            collation=None,
        )],
    )

    sql = SqlRendererV3(contract, binding).render_query(
        compile_contract_plan(contract, binding)
    )
    expected = compose_expected_rows(
        contract,
        {"sales_fact": [{"revenue": Decimal("125.50")}]},
    )

    assert "SUM(" in sql
    assert expected == [{"TotalRevenue": Decimal("125.50")}]


def test_source_facts_are_compiled_preflighted_and_frozen_independently():
    contract, binding = reconciliation_contract_and_binding()
    drafts = [
        ReferenceFactDraft(
            fact_id=fact.id,
            meaning=fact.meaning,
            actual_projection=[],
            plan=compile_fact_plan(contract, binding, fact),
            comparator=None,
            comparison_role="source_fact",
        )
        for fact in contract.facts
    ]

    def compiler(_artifact, _name, sql, _parameters):
        if "CoverageCount" in sql:
            columns = [{
                "name": "CoverageCount",
                "sql_type": "bigint",
                "nullable": False,
            }]
        else:
            columns = [
                {
                    "name": "period",
                    "sql_type": "nvarchar",
                    "nullable": True,
                },
                {
                    "name": "revenue",
                    "sql_type": "decimal",
                    "nullable": True,
                },
            ]
        return {"ok": True, "result_columns": columns}

    def preflight(sql, _parameters):
        if "CoverageCount" in sql:
            return [{"CoverageCount": 1}]
        return [{"period": "2026-01", "revenue": Decimal("100")}]

    bundle = freeze_reference_bundle(
        contract,
        binding,
        drafts,
        [ValidationCase(
            case_id="coverage",
            kind="coverage",
            parameters={},
        )],
        compiler=compiler,
        preflight_executor=preflight,
        result_comparator=ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=["Period"],
            compare_columns=[
                "SalesRevenue", "JournalRevenue", "Difference",
            ],
        ),
    )

    assert bundle.status == "reference_ready"
    assert all(
        fact.comparison_role == "source_fact"
        and fact.coverage_sql is not None
        for fact in bundle.facts
    )


def test_fact_measure_can_aggregate_expression_over_multiple_source_fields():
    payload = reconciliation_payload()
    payload["source_fields"].extend([
        {
            "id": "journal_debit",
            "entity_id": "journal",
            "meaning": "凭证借方系统币金额",
            "logical_type": "money",
        },
        {
            "id": "journal_credit",
            "entity_id": "journal",
            "meaning": "凭证贷方系统币金额",
            "logical_type": "money",
        },
    ])
    measure = payload["facts"][1]["measures"][0]
    measure.pop("field_id")
    measure["expression"] = {
        "kind": "binary",
        "operator": "-",
        "args": [
            {"kind": "field", "field_id": "journal_credit"},
            {"kind": "field", "field_id": "journal_debit"},
        ],
    }
    contract = SemanticContract.model_validate(payload)
    _, original_binding = reconciliation_contract_and_binding()
    binding = original_binding.model_copy(update={
        "contract_hash": contract.content_hash,
        "fields": original_binding.fields + [
            FieldBinding(
                binding_id="journal_debit_binding",
                semantic_id="journal_debit",
                entity_id="journal",
                column="Debit",
                column_id=3,
                sql_type="decimal",
                nullable=True,
                collation=None,
            ),
            FieldBinding(
                binding_id="journal_credit_binding",
                semantic_id="journal_credit",
                entity_id="journal",
                column="Credit",
                column_id=4,
                sql_type="decimal",
                nullable=True,
                collation=None,
            ),
        ],
    })

    sql = SqlRendererV3(contract, binding).render_query(
        compile_fact_plan(contract, binding, contract.facts[1])
    )

    assert "SUM((" in sql
    assert "[src].[journal_credit_binding]" in sql
    assert "[src].[journal_debit_binding]" in sql


def test_result_binding_can_reference_another_output_without_sibling_alias_sql():
    payload = reconciliation_payload()
    payload["result_bindings"][3]["expression"] = {
        "kind": "binary",
        "operator": "-",
        "args": [
            {"kind": "output", "output_id": "sales_revenue"},
            {"kind": "output", "output_id": "journal_revenue"},
        ],
    }
    contract = SemanticContract.model_validate(payload)
    _, original_binding = reconciliation_contract_and_binding()
    binding = original_binding.model_copy(
        update={"contract_hash": contract.content_hash}
    )

    sql = SqlRendererV3(contract, binding).render_query(
        compile_contract_plan(contract, binding)
    )
    rows = compose_expected_rows(
        contract,
        {
            "sales_fact": [{"period": "2026-01", "revenue": Decimal("100")}],
            "journal_fact": [{"period": "2026-01", "revenue": Decimal("90")}],
        },
    )

    assert "AS [Difference]" in sql
    assert rows[0]["Difference"] == Decimal("10")


def test_result_output_dependency_cycle_is_rejected():
    payload = reconciliation_payload()
    payload["result_bindings"][1]["expression"] = {
        "kind": "output",
        "output_id": "difference",
    }
    payload["result_bindings"][3]["expression"] = {
        "kind": "output",
        "output_id": "sales_revenue",
    }

    with pytest.raises(ValueError, match="循环输出依赖"):
        SemanticContract.model_validate(payload)


def test_result_expression_parameter_is_shared_by_sql_and_expected_composer():
    payload = reconciliation_payload()
    payload["parameters"] = [{
        "id": "tolerance_amount",
        "name": "@ToleranceAmount",
        "logical_type": "money",
        "required": True,
        "default": 0.01,
        "meaning": "金额差异容差",
        "boundary": "none",
    }]
    payload["outputs"].append({
        "id": "within_tolerance",
        "name": "WithinTolerance",
        "meaning": "差额是否在容差内",
        "logical_type": "boolean",
        "nullable": False,
    })
    payload["result_bindings"].append({
        "output_id": "within_tolerance",
        "expression": {
            "kind": "binary",
            "operator": "<=",
            "args": [
                {
                    "kind": "function",
                    "operator": "ABS",
                    "args": [{
                        "kind": "output",
                        "output_id": "difference",
                    }],
                },
                {
                    "kind": "parameter",
                    "parameter_id": "tolerance_amount",
                },
            ],
        },
    })
    contract = SemanticContract.model_validate(payload)
    _, original_binding = reconciliation_contract_and_binding()
    binding = original_binding.model_copy(
        update={"contract_hash": contract.content_hash}
    )

    sql = SqlRendererV3(contract, binding).render_query(
        compile_contract_plan(contract, binding)
    )
    rows = compose_expected_rows(
        contract,
        {
            "sales_fact": [{"period": "2026-01", "revenue": Decimal("100")}],
            "journal_fact": [{"period": "2026-01", "revenue": Decimal("99.5")}],
        },
        {"tolerance_amount": Decimal("1")},
    )

    assert "@ToleranceAmount" in sql
    assert rows[0]["WithinTolerance"] is True


def test_multi_source_expected_is_composed_without_reading_sp_sql():
    contract, _binding = reconciliation_contract_and_binding()
    rows = compose_expected_rows(
        contract,
        {
            "sales_fact": [
                {"period": "2026-01", "revenue": Decimal("100")},
                {"period": "2026-02", "revenue": Decimal("50")},
            ],
            "journal_fact": [
                {"period": "2026-01", "revenue": Decimal("90")},
                {"period": "2026-03", "revenue": Decimal("20")},
            ],
        },
    )

    by_period = {row["Period"]: row for row in rows}
    assert by_period["2026-01"]["Difference"] == Decimal("10")
    assert by_period["2026-02"]["JournalRevenue"] is None
    assert by_period["2026-03"]["SalesRevenue"] is None
    assert len(rows) == 3


def test_exception_result_filter_is_shared_by_sql_and_memory_composition():
    payload = reconciliation_payload()
    payload["result_mode"] = "exception_rows"
    payload["result_filter"] = {
        "kind": "binary",
        "operator": "<>",
        "args": [
            {
                "kind": "fact_value",
                "fact_value": {
                    "fact_id": "sales_fact",
                    "value_id": "revenue",
                },
            },
            {
                "kind": "fact_value",
                "fact_value": {
                    "fact_id": "journal_fact",
                    "value_id": "revenue",
                },
            },
        ],
    }
    contract = SemanticContract.model_validate(payload)
    _, original_binding = reconciliation_contract_and_binding()
    binding = original_binding.model_copy(
        update={"contract_hash": contract.content_hash}
    )

    sql = SqlRendererV3(contract, binding).render_query(
        compile_contract_plan(contract, binding)
    )
    rows = compose_expected_rows(
        contract,
        {
            "sales_fact": [
                {"period": "2026-01", "revenue": Decimal("100")},
                {"period": "2026-02", "revenue": Decimal("50")},
            ],
            "journal_fact": [
                {"period": "2026-01", "revenue": Decimal("100")},
                {"period": "2026-02", "revenue": Decimal("40")},
            ],
        },
    )

    assert "WHERE" in sql
    assert [row["Period"] for row in rows] == ["2026-02"]


def reconciliation_catalog():
    columns = [
        CatalogColumn(
            column_id=1,
            name="PeriodCode",
            sql_type="nvarchar",
            max_length=20,
            precision=0,
            scale=0,
            nullable=False,
            collation=None,
        ),
        CatalogColumn(
            column_id=2,
            name="Amount",
            sql_type="decimal",
            max_length=9,
            precision=19,
            scale=4,
            nullable=False,
            collation=None,
        ),
    ]
    return CatalogSnapshot(
        server_identity="TEST-SQL",
        database_name="TEST_DB",
        database_id=7,
        compatibility_level=160,
        database_collation="Chinese_PRC_CI_AS",
        default_schema="dbo",
        current_user="tester",
        can_read_catalog=True,
        captured_at=datetime.now(timezone.utc),
        objects=[
            CatalogObject(
                schema="dbo",
                name="SalesIncome",
                object_id=101,
                object_type="table",
                columns=columns,
            ),
            CatalogObject(
                schema="dbo",
                name="JournalIncome",
                object_id=102,
                object_type="table",
                columns=columns,
            ),
        ],
    )


def reconciliation_artifacts():
    contract, binding = reconciliation_contract_and_binding()
    catalog = reconciliation_catalog()
    binding = binding.model_copy(
        update={"catalog_fingerprint": catalog_fingerprint(catalog)}
    )
    renderer = SqlRendererV3(contract, binding)
    reference_facts = []
    for fact in contract.facts:
        plan = compile_fact_plan(contract, binding, fact)
        source_coverage_plan = coverage_plan(plan, binding)
        reference_facts.append(
            ReferenceFact(
                fact_id=fact.id,
                meaning=fact.meaning,
                comparison_role="source_fact",
                reference_plan=plan,
                expected_sql=renderer.render_query(plan),
                coverage_sql=renderer.render_query(source_coverage_plan),
                expected_schema=plan.result_schema,
                allowed_object_ids=[
                    101 if fact.id == "sales_fact" else 102
                ],
            )
        )
    result_comparator = ComparatorSpec(
        type="keyed_rows_equal",
        key_columns=["Period"],
        compare_columns=[
            "SalesRevenue", "JournalRevenue", "Difference",
        ],
        tolerance={
            "SalesRevenue": 0.01,
            "JournalRevenue": 0.01,
            "Difference": 0.01,
        },
    )
    reference = ReferenceBundle(
        contract_hash=contract.content_hash,
        binding_hash=binding.content_hash,
        renderer_version="sqlserver-relational-v3.1",
        facts=reference_facts,
        result_comparator=result_comparator,
        validation_cases=[
            ValidationCase(
                case_id="coverage_data",
                kind="coverage",
                parameters={},
            )
        ],
        compile_evidence={
            key: {"ok": True}
            for fact in reference_facts
            for key in (fact.fact_id, f"{fact.fact_id}:coverage")
        },
        preflight_evidence={
            fact.fact_id: {
                "executed": True,
                "row_count": 1,
                "source_row_count": 1,
            }
            for fact in reference_facts
        },
        status="reference_ready",
    )
    plan = compile_contract_plan(contract, binding)
    candidate = generate_procedure_candidate(
        contract,
        binding,
        reference,
        lambda _contract, _binding: plan,
        compiler=lambda *args: {"ok": True},
    )
    return contract, catalog, binding, reference, candidate


class ReconciliationExecutor:
    def __init__(self, actual_rows):
        self.actual_rows = actual_rows

    def inspect_environment(self):
        return {
            "server_identity": "TEST-SQL",
            "database_name": "TEST_DB",
            "database_id": 7,
            "snapshot_isolation_state": "ON",
        }

    def execute_same_snapshot(
        self,
        contract,
        reference_sql,
        actual_procedure_sql,
        actual_procedure_name,
        parameters,
    ):
        assert set(reference_sql) == {
            "sales_fact",
            "sales_fact:coverage",
            "journal_fact",
            "journal_fact:coverage",
        }
        return SnapshotExecution(
            snapshot_id="tx-reconciliation",
            database_identity="TEST-SQL/TEST_DB/7",
            references={
                "sales_fact": RuntimeResult(
                    columns=("period", "revenue"),
                    rows=[
                        {
                            "period": "2026-01",
                            "revenue": Decimal("100"),
                        }
                    ],
                ),
                "journal_fact": RuntimeResult(
                    columns=("period", "revenue"),
                    rows=[
                        {
                            "period": "2026-01",
                            "revenue": Decimal("90"),
                        }
                    ],
                ),
                "sales_fact:coverage": RuntimeResult(
                    columns=("CoverageCount",),
                    rows=[{"CoverageCount": 1}],
                ),
                "journal_fact:coverage": RuntimeResult(
                    columns=("CoverageCount",),
                    rows=[{"CoverageCount": 1}],
                ),
            },
            actual=RuntimeResult(
                columns=(
                    "Period", "SalesRevenue",
                    "JournalRevenue", "Difference",
                ),
                rows=self.actual_rows,
            ),
        )


def test_composed_reference_validates_final_sp_result():
    contract, catalog, binding, reference, candidate = (
        reconciliation_artifacts()
    )
    evidence = validate_candidate_v3(
        contract,
        catalog,
        binding,
        reference,
        candidate,
        executor=ReconciliationExecutor([
            {
                "Period": "2026-01",
                "SalesRevenue": Decimal("100"),
                "JournalRevenue": Decimal("90"),
                "Difference": Decimal("10"),
            }
        ]),
    )

    assert evidence.status == "validated"
    assert evidence.comparisons[0].fact_id == "composed_expected"


def test_composed_reference_rejects_wrong_final_sp_result():
    contract, catalog, binding, reference, candidate = (
        reconciliation_artifacts()
    )
    evidence = validate_candidate_v3(
        contract,
        catalog,
        binding,
        reference,
        candidate,
        executor=ReconciliationExecutor([
            {
                "Period": "2026-01",
                "SalesRevenue": Decimal("100"),
                "JournalRevenue": Decimal("90"),
                "Difference": Decimal("9"),
            }
        ]),
    )

    assert evidence.status == "failed"
    assert not evidence.comparisons[0].match
