from datetime import datetime, timezone

from app.contracts.reference import ComparatorSpec, ReferenceBundle, ReferenceFact, ValidationCase
from app.contracts.relational_plan import (
    Expression,
    NamedExpression,
    PlanNode,
    RelationalPlan,
    ResultColumn,
)
from app.contracts.schema import (
    CatalogColumn,
    CatalogObject,
    CatalogSnapshot,
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingProposal,
)
from app.contracts.semantic import (
    SemanticContract,
    SemanticEntity,
    SemanticFilter,
    SemanticOutput,
    SemanticParameter,
)
from app.services.schema_binding_v3 import build_schema_binding
from app.services.sql_renderer_v3 import RENDERER_VERSION, SqlRendererV3


def contract():
    return SemanticContract(
        contract_id="invoice_income",
        procedure_name="usp_invoice_income",
        purpose="按日期返回应收发票收入",
        result_mode="full_rows",
        parameters=[
            SemanticParameter(
                id="from_date",
                name="@FromDate",
                logical_type="date",
                required=True,
                default=None,
                meaning="开始日期",
                boundary="inclusive",
            ),
            SemanticParameter(
                id="to_date",
                name="@ToDate",
                logical_type="date",
                required=True,
                default=None,
                meaning="结束日期",
                boundary="inclusive_full_day",
            ),
        ],
        entities=[SemanticEntity(id="invoice", meaning="应收发票")],
        grain=["invoice_id"],
        outputs=[
            SemanticOutput(
                id="invoice_id",
                name="InvoiceId",
                meaning="发票主键",
                logical_type="integer",
                nullable=False,
            ),
            SemanticOutput(
                id="amount",
                name="Amount",
                meaning="收入金额",
                logical_type="money",
                nullable=False,
            ),
        ],
        filters=[
            SemanticFilter(
                id="invoice_date_range",
                meaning="发票日期覆盖起止自然日",
                field_ids=["invoice_date"],
                parameter_ids=["from_date", "to_date"],
                operator="full_day_range",
            )
        ],
    )


def catalog():
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
                name="OINV",
                object_id=101,
                object_type="table",
                columns=[
                    CatalogColumn(
                        column_id=1,
                        name="DocEntry",
                        sql_type="int",
                        max_length=4,
                        precision=10,
                        scale=0,
                        nullable=False,
                        collation=None,
                    ),
                    CatalogColumn(
                        column_id=2,
                        name="DocDate",
                        sql_type="datetime",
                        max_length=8,
                        precision=23,
                        scale=3,
                        nullable=False,
                        collation=None,
                    ),
                    CatalogColumn(
                        column_id=3,
                        name="DocTotal",
                        sql_type="decimal",
                        max_length=9,
                        precision=19,
                        scale=6,
                        nullable=False,
                        collation=None,
                    ),
                    CatalogColumn(
                        column_id=4,
                        name="CardCode",
                        sql_type="nvarchar",
                        max_length=30,
                        precision=0,
                        scale=0,
                        nullable=False,
                        collation="Latin1_General_CI_AS",
                    ),
                ],
                primary_key=[1],
            )
        ],
    )


def binding(contract_value=None, catalog_value=None):
    contract_value = contract_value or contract()
    catalog_value = catalog_value or catalog()
    proposal = SchemaBindingProposal(
        entities=[
            EntityBindingProposal(
                entity_id="invoice",
                database="TEST_DB",
                schema="dbo",
                object="OINV",
                alias="i",
            )
        ],
        fields=[
            FieldBindingProposal(
                binding_id="invoice_id",
                semantic_id="invoice_id",
                entity_id="invoice",
                column="DocEntry",
            ),
            FieldBindingProposal(
                binding_id="invoice_date",
                semantic_id="invoice_date",
                entity_id="invoice",
                column="DocDate",
            ),
            FieldBindingProposal(
                binding_id="invoice_amount",
                semantic_id="amount",
                entity_id="invoice",
                column="DocTotal",
            ),
            FieldBindingProposal(
                binding_id="customer_code",
                semantic_id="customer_code",
                entity_id="invoice",
                column="CardCode",
            ),
        ],
    )
    return build_schema_binding(contract_value, catalog_value, proposal)


def plan():
    scan = PlanNode(node_id="scan_invoice", kind="scan", entity_id="invoice")
    lower = Expression(
        kind="binary",
        operator=">=",
        args=[
            Expression(kind="column", field_binding_id="invoice_date"),
            Expression(kind="parameter", parameter_id="from_date"),
        ],
    )
    upper = Expression(
        kind="binary",
        operator="<",
        args=[
            Expression(kind="column", field_binding_id="invoice_date"),
            Expression(
                kind="function",
                operator="DATEADD",
                args=[
                    Expression(kind="literal", value="day", value_type="string"),
                    Expression(kind="literal", value=1, value_type="integer"),
                    Expression(kind="parameter", parameter_id="to_date"),
                ],
            ),
        ],
    )
    filtered = PlanNode(
        node_id="filter_dates",
        kind="filter",
        input=scan,
        predicate=Expression(
            kind="binary",
            operator="AND",
            args=[lower, upper],
        ),
    )
    projected = PlanNode(
        node_id="project_result",
        kind="project",
        input=filtered,
        projections=[
            NamedExpression(
                name="InvoiceId",
                expression=Expression(
                    kind="column",
                    field_binding_id="invoice_id",
                ),
            ),
            NamedExpression(
                name="Amount",
                expression=Expression(
                    kind="column",
                    field_binding_id="invoice_amount",
                ),
            ),
        ],
    )
    return RelationalPlan(
        plan_id="invoice_reference",
        purpose="发票收入",
        root=projected,
        result_schema=[
            ResultColumn(name="InvoiceId", logical_type="integer", nullable=False),
            ResultColumn(name="Amount", logical_type="money", nullable=False),
        ],
    )


def reference_bundle(contract_value=None, binding_value=None, plan_value=None):
    contract_value = contract_value or contract()
    binding_value = binding_value or binding(contract_value)
    plan_value = plan_value or plan()
    sql = SqlRendererV3(contract_value, binding_value).render_query(plan_value)
    fact = ReferenceFact(
        fact_id="invoice_income",
        meaning="应收发票收入",
        actual_projection=["InvoiceId", "Amount"],
        reference_plan=plan_value,
        expected_sql=sql,
        expected_schema=plan_value.result_schema,
        comparator=ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=["InvoiceId"],
            compare_columns=["Amount"],
            tolerance={"Amount": 0.01},
        ),
        allowed_object_ids=[101],
    )
    return ReferenceBundle(
        contract_hash=contract_value.content_hash,
        binding_hash=binding_value.content_hash,
        renderer_version=RENDERER_VERSION,
        facts=[fact],
        validation_cases=[
            ValidationCase(
                case_id="coverage_2026",
                kind="coverage",
                parameters={
                    "from_date": "2026-01-01",
                    "to_date": "2026-01-31",
                },
            )
        ],
        compile_evidence={"invoice_income": {"ok": True}},
        preflight_evidence={
            "invoice_income": {
                "executed": True,
                "row_count": 1,
                "case_id": "coverage_2026",
            }
        },
        status="reference_ready",
    )
