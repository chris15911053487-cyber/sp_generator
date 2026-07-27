"""真实 SQL Server V3 验收；只允许显式隔离测试库运行。"""

import json
import os

import pytest

from app.contracts.reference import ComparatorSpec, ValidationCase
from app.contracts.relational_plan import (
    Expression,
    NamedExpression,
    PlanNode,
    RelationalPlan,
    ResultColumn,
)
from app.contracts.schema import (
    EntityBindingProposal,
    FieldBindingProposal,
    SchemaBindingProposal,
)
from app.contracts.semantic import (
    SemanticContract,
    SemanticEntity,
    SemanticFact,
    SemanticFactDimension,
    SemanticFactJoin,
    SemanticFactJoinKey,
    SemanticFactMeasure,
    SemanticFactValueRef,
    SemanticOutput,
    SemanticResultBinding,
    SemanticResultExpression,
    SemanticSourceField,
)
from app.db.sqlserver import get_connection
from app.services.catalog_v3 import capture_catalog_snapshot
from app.services.fact_compiler_v3 import (
    compile_contract_plan,
    compile_fact_plan,
)
from app.services.procedure_generator_v3 import generate_procedure_candidate
from app.services.reference_planner import (
    ReferenceFactDraft,
    freeze_reference_bundle,
)
from app.services.schema_binding_v3 import build_schema_binding
from app.services.validation_runner_v3 import (
    SqlServerValidationExecutor,
    validate_candidate_v3,
)
from config import get_db_config, is_explicit_test_database


pytestmark = pytest.mark.skipif(
    os.getenv("RUN_V3_E2E") != "1",
    reason="需要显式授权的隔离 SQL Server 测试库",
)


def _require_test_database():
    if not is_explicit_test_database(get_db_config()):
        pytest.fail("V3 E2E 只允许 environment=test 的隔离数据库")


@pytest.fixture(scope="module", autouse=True)
def business_fixture():
    _require_test_database()
    connection = get_connection(autocommit=True)
    cursor = connection.cursor()
    cursor.execute("DROP TABLE IF EXISTS dbo.__v3_journal_test")
    cursor.execute("DROP TABLE IF EXISTS dbo.__v3_invoice_line_test")
    cursor.execute("DROP TABLE IF EXISTS dbo.__v3_invoice_test")
    cursor.execute(
        "CREATE TABLE dbo.__v3_invoice_test ("
        "InvoiceId int NOT NULL PRIMARY KEY, CustomerCode nvarchar(20) NOT NULL, "
        "Amount decimal(19,4) NOT NULL)"
    )
    cursor.execute(
        "CREATE TABLE dbo.__v3_invoice_line_test ("
            "InvoiceId int NOT NULL, [LineNo] int NOT NULL, "
        "LineAmount decimal(19,4) NOT NULL, "
            "CONSTRAINT PK___v3_invoice_line_test PRIMARY KEY (InvoiceId, [LineNo]), "
        "CONSTRAINT FK___v3_invoice_line_test FOREIGN KEY (InvoiceId) "
        "REFERENCES dbo.__v3_invoice_test(InvoiceId))"
    )
    cursor.execute(
        "CREATE TABLE dbo.__v3_journal_test ("
        "InvoiceId int NOT NULL PRIMARY KEY, JournalAmount decimal(19,4) NOT NULL)"
    )
    cursor.execute(
        "INSERT dbo.__v3_invoice_test VALUES "
        "(1,N'C001',100.00),(2,N'C002',200.00)"
    )
    cursor.execute(
        "INSERT dbo.__v3_invoice_line_test VALUES "
        "(1,0,40.00),(1,1,60.00),(2,0,200.00)"
    )
    cursor.execute(
        "INSERT dbo.__v3_journal_test VALUES (1,100.00),(2,200.00)"
    )
    connection.close()
    try:
        yield
    finally:
        connection = get_connection(autocommit=True)
        cursor = connection.cursor()
        cursor.execute("DROP TABLE IF EXISTS dbo.__v3_journal_test")
        cursor.execute("DROP TABLE IF EXISTS dbo.__v3_invoice_line_test")
        cursor.execute("DROP TABLE IF EXISTS dbo.__v3_invoice_test")
        connection.close()


def _column(binding_id):
    return Expression(kind="column", field_binding_id=binding_id)


def _project(plan_id, purpose, input_node, outputs):
    return RelationalPlan(
        plan_id=plan_id,
        purpose=purpose,
        root=PlanNode(
            node_id=plan_id + "_project",
            kind="project",
            input=input_node,
            projections=[
                NamedExpression(name=name, expression=expression)
                for name, expression, _logical_type in outputs
            ],
        ),
        result_schema=[
            ResultColumn(name=name, logical_type=logical_type, nullable=False)
            for name, _expression, logical_type in outputs
        ],
    )


def _contract(
    name,
    purpose,
    entities,
    outputs,
    grain,
    *,
    source_fields=None,
    facts=None,
    fact_joins=None,
    result_bindings=None,
):
    return SemanticContract(
        contract_id=name,
        procedure_name=name,
        purpose=purpose,
        result_mode="full_rows",
        entities=[
            SemanticEntity(id=entity_id, meaning=meaning)
            for entity_id, meaning in entities
        ],
        outputs=[
            SemanticOutput(
                id=output_id,
                name=column_name,
                meaning=meaning,
                logical_type=logical_type,
                nullable=(
                    item[4] if len(item) > 4 else False
                ),
            )
            for item in outputs
            for output_id, column_name, meaning, logical_type in [item[:4]]
        ],
        grain=grain,
        source_fields=source_fields or [],
        facts=facts or [],
        fact_joins=fact_joins or [],
        result_bindings=result_bindings or [],
        allow_empty=False,
    )


def _proposal(contract, entities, fields, joins=None):
    return SchemaBindingProposal(
        entities=[
            EntityBindingProposal(
                entity_id=entity_id,
                database=get_db_config()["database"],
                schema="dbo",
                object=table,
                alias=alias,
            )
            for entity_id, table, alias in entities
        ],
        fields=[
            FieldBindingProposal(
                binding_id=binding_id,
                semantic_id=semantic_id,
                entity_id=entity_id,
                column=column,
            )
            for binding_id, semantic_id, entity_id, column in fields
        ],
        joins=joins or [],
    )


def _validate(
    contract,
    proposal,
    reference_drafts,
    procedure_plan,
    *,
    result_comparator=None,
):
    catalog = capture_catalog_snapshot()
    binding = build_schema_binding(contract, catalog, proposal)
    executor = SqlServerValidationExecutor()
    case = ValidationCase(
        case_id="coverage_fixture",
        kind="coverage",
        parameters={},
        selection_evidence={"source": "isolated_fixture"},
    )
    reference = freeze_reference_bundle(
        contract,
        binding,
        [
            draft(binding) if callable(draft) else draft
            for draft in reference_drafts
        ],
        [case],
        preflight_executor=lambda sql, parameters: executor.preflight_reference(
            contract, sql, parameters
        ),
        result_comparator=result_comparator,
    )
    candidate = generate_procedure_candidate(
        contract,
        binding,
        reference,
        lambda *_args: procedure_plan(binding)
        if callable(procedure_plan) else procedure_plan,
    )
    evidence = validate_candidate_v3(
        contract,
        catalog,
        binding,
        reference,
        candidate,
        executor=executor,
    )
    assert evidence.status == "validated", json.dumps(
        evidence.model_dump(mode="json"),
        ensure_ascii=False,
        default=str,
    )


def test_detail_and_header_line_queries_validate_end_to_end():
    detail = _contract(
        "usp_v3_invoice_detail",
        "应收发票头明细",
        [("invoice", "应收发票头")],
        [
            ("invoice_id", "InvoiceId", "发票编号", "integer"),
            ("amount", "Amount", "发票金额", "money"),
        ],
        ["invoice_id"],
    )
    detail_proposal = _proposal(
        detail,
        [("invoice", "__v3_invoice_test", "i")],
        [
            ("invoice_id", "invoice_id", "invoice", "InvoiceId"),
            ("amount", "amount", "invoice", "Amount"),
        ],
    )
    detail_plan = _project(
        "invoice_detail",
        detail.purpose,
        PlanNode(node_id="scan_invoice", kind="scan", entity_id="invoice"),
        [
            ("InvoiceId", _column("invoice_id"), "integer"),
            ("Amount", _column("amount"), "money"),
        ],
    )
    comparator = ComparatorSpec(
        type="keyed_rows_equal",
        key_columns=["InvoiceId"],
        compare_columns=["Amount"],
        tolerance={"Amount": 0.01},
    )
    draft = ReferenceFactDraft(
        fact_id="invoice_income",
        meaning="发票收入事实",
        actual_projection=["InvoiceId", "Amount"],
        plan=detail_plan,
        comparator=comparator,
    )
    _validate(detail, detail_proposal, [draft], detail_plan)

    line_contract = _contract(
        "usp_v3_invoice_line_detail",
        "应收发票头行明细",
        [("invoice", "应收发票头"), ("line", "应收发票行")],
        [
            ("invoice_id", "InvoiceId", "发票编号", "integer"),
            ("line_no", "LineNo", "发票行号", "integer"),
            ("line_amount", "LineAmount", "发票行金额", "money"),
        ],
        ["invoice_id", "line_no"],
        source_fields=[
            SemanticSourceField(
                id="invoice_id", entity_id="invoice",
                meaning="发票编号", logical_type="integer", nullable=False,
            ),
            SemanticSourceField(
                id="line_no", entity_id="line",
                meaning="发票行号", logical_type="integer", nullable=False,
            ),
            SemanticSourceField(
                id="line_invoice_id", entity_id="line",
                meaning="发票行关联的发票编号",
                logical_type="integer", nullable=False,
            ),
            SemanticSourceField(
                id="line_amount", entity_id="line",
                meaning="发票行金额", logical_type="money", nullable=False,
            ),
        ],
        facts=[
            SemanticFact(
                id="invoice_line",
                meaning="应收发票头行收入事实",
                entity_ids=["invoice", "line"],
                dimensions=[
                    SemanticFactDimension(
                        id="invoice_id", field_id="invoice_id",
                        meaning="发票编号",
                    ),
                    SemanticFactDimension(
                        id="line_no", field_id="line_no",
                        meaning="发票行号",
                    ),
                ],
                measures=[
                    SemanticFactMeasure(
                        id="line_amount", field_id="line_amount",
                        meaning="发票行金额", aggregation="none",
                        logical_type="money",
                    ),
                ],
                grain=["invoice_id", "line_no"],
            ),
        ],
        result_bindings=[
            SemanticResultBinding(
                output_id=value,
                expression=SemanticResultExpression(
                    kind="fact_value",
                    fact_value=SemanticFactValueRef(
                        fact_id="invoice_line", value_id=value,
                    ),
                ),
            )
            for value in ("invoice_id", "line_no", "line_amount")
        ],
    )
    from app.contracts.schema import JoinBindingProposal

    line_proposal = _proposal(
        line_contract,
        [
            ("invoice", "__v3_invoice_test", "i"),
            ("line", "__v3_invoice_line_test", "l"),
        ],
        [
            ("invoice_id", "invoice_id", "invoice", "InvoiceId"),
            ("line_invoice_id", "line_invoice_id", "line", "InvoiceId"),
            ("line_no", "line_no", "line", "LineNo"),
            ("line_amount", "line_amount", "line", "LineAmount"),
        ],
        joins=[
            JoinBindingProposal(
                id="invoice_lines",
                left_entity="invoice",
                left_field_binding_id="invoice_id",
                right_entity="line",
                right_field_binding_id="line_invoice_id",
                join_type="inner",
                evidence="foreign_key",
                meaning="发票头与发票行的外键关系",
            )
        ],
    )
    _validate(
        line_contract,
        line_proposal,
        [
            lambda binding: ReferenceFactDraft(
                fact_id="invoice_line",
                meaning="应收发票头行收入事实",
                actual_projection=[],
                plan=compile_fact_plan(
                    line_contract,
                    binding,
                    line_contract.facts[0],
                ),
                comparator=None,
                comparison_role="source_fact",
            )
        ],
        lambda binding: compile_contract_plan(line_contract, binding),
        result_comparator=ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=["InvoiceId", "LineNo"],
            compare_columns=["LineAmount"],
            tolerance={"LineAmount": 0.01},
        ),
    )


def test_grouped_summary_validates_end_to_end():
    contract = _contract(
        "usp_v3_customer_summary",
        "按客户汇总应收收入",
        [("invoice", "应收发票头")],
        [
            ("customer", "CustomerCode", "客户编码", "string"),
            ("amount", "Amount", "客户收入", "money", True),
        ],
        ["customer"],
    )
    proposal = _proposal(
        contract,
        [("invoice", "__v3_invoice_test", "i")],
        [
            ("customer", "customer", "invoice", "CustomerCode"),
            ("amount", "amount", "invoice", "Amount"),
        ],
    )
    plan = RelationalPlan(
        plan_id="customer_summary",
        purpose=contract.purpose,
        root=PlanNode(
            node_id="aggregate_customer",
            kind="aggregate",
            input=PlanNode(
                node_id="scan_invoice", kind="scan", entity_id="invoice"
            ),
            group_by=[
                NamedExpression(
                    name="CustomerCode", expression=_column("customer")
                )
            ],
            aggregates=[
                NamedExpression(
                    name="Amount",
                    expression=Expression(
                        kind="function",
                        operator="SUM",
                        args=[_column("amount")],
                    ),
                )
            ],
        ),
        result_schema=[
            ResultColumn(
                name="CustomerCode", logical_type="string", nullable=False
            ),
            ResultColumn(name="Amount", logical_type="money", nullable=True),
        ],
    )
    draft = ReferenceFactDraft(
        fact_id="customer_income",
        meaning="客户收入汇总事实",
        actual_projection=["CustomerCode", "Amount"],
        plan=plan,
        comparator=ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=["CustomerCode"],
            compare_columns=["Amount"],
            tolerance={"Amount": 0.01},
        ),
    )
    _validate(contract, proposal, [draft], plan)


def test_reconciliation_uses_two_independent_facts_end_to_end():
    contract = _contract(
        "usp_v3_income_reconciliation",
        "应收发票收入与凭证收入对账",
        [("invoice", "业务端应收发票"), ("journal", "财务端收入凭证")],
        [
            ("invoice_id", "InvoiceId", "稳定关联键", "integer"),
            ("invoice_amount", "InvoiceAmount", "发票收入", "money"),
            ("journal_amount", "JournalAmount", "凭证收入", "money"),
        ],
        ["invoice_id"],
        source_fields=[
            SemanticSourceField(
                id="invoice_id", entity_id="invoice",
                meaning="发票编号", logical_type="integer", nullable=False,
            ),
            SemanticSourceField(
                id="invoice_amount", entity_id="invoice",
                meaning="发票收入", logical_type="money", nullable=False,
            ),
            SemanticSourceField(
                id="journal_invoice_id", entity_id="journal",
                meaning="凭证关联发票编号",
                logical_type="integer", nullable=False,
            ),
            SemanticSourceField(
                id="journal_amount", entity_id="journal",
                meaning="凭证收入", logical_type="money", nullable=False,
            ),
        ],
        facts=[
            SemanticFact(
                id="invoice_revenue",
                meaning="业务端发票收入事实",
                entity_ids=["invoice"],
                dimensions=[
                    SemanticFactDimension(
                        id="invoice_id", field_id="invoice_id",
                        meaning="发票编号",
                    ),
                ],
                measures=[
                    SemanticFactMeasure(
                        id="invoice_amount", field_id="invoice_amount",
                        meaning="发票收入", aggregation="none",
                        logical_type="money",
                    ),
                ],
                grain=["invoice_id"],
            ),
            SemanticFact(
                id="journal_revenue",
                meaning="财务端凭证收入事实",
                entity_ids=["journal"],
                dimensions=[
                    SemanticFactDimension(
                        id="journal_invoice_id",
                        field_id="journal_invoice_id",
                        meaning="凭证关联发票编号",
                    ),
                ],
                measures=[
                    SemanticFactMeasure(
                        id="journal_amount", field_id="journal_amount",
                        meaning="凭证收入", aggregation="none",
                        logical_type="money",
                    ),
                ],
                grain=["journal_invoice_id"],
            ),
        ],
        fact_joins=[
            SemanticFactJoin(
                id="invoice_journal",
                join_type="inner",
                meaning="按发票编号关联业务与财务收入",
                keys=[
                    SemanticFactJoinKey(
                        left=SemanticFactValueRef(
                            fact_id="invoice_revenue",
                            value_id="invoice_id",
                        ),
                        right=SemanticFactValueRef(
                            fact_id="journal_revenue",
                            value_id="journal_invoice_id",
                        ),
                    ),
                ],
            ),
        ],
        result_bindings=[
            SemanticResultBinding(
                output_id=output_id,
                expression=SemanticResultExpression(
                    kind="fact_value",
                    fact_value=SemanticFactValueRef(
                        fact_id=fact_id,
                        value_id=value_id,
                    ),
                ),
            )
            for output_id, fact_id, value_id in (
                ("invoice_id", "invoice_revenue", "invoice_id"),
                ("invoice_amount", "invoice_revenue", "invoice_amount"),
                ("journal_amount", "journal_revenue", "journal_amount"),
            )
        ],
    )
    from app.contracts.schema import JoinBindingProposal

    proposal = _proposal(
        contract,
        [
            ("invoice", "__v3_invoice_test", "i"),
            ("journal", "__v3_journal_test", "j"),
        ],
        [
            ("invoice_id", "invoice_id", "invoice", "InvoiceId"),
            (
                "journal_invoice_id",
                "journal_invoice_id",
                "journal",
                "InvoiceId",
            ),
            ("invoice_amount", "invoice_amount", "invoice", "Amount"),
            (
                "journal_amount",
                "journal_amount",
                "journal",
                "JournalAmount",
            ),
        ],
        joins=[
            JoinBindingProposal(
                id="invoice_journal",
                left_entity="invoice",
                left_field_binding_id="invoice_id",
                right_entity="journal",
                right_field_binding_id="journal_invoice_id",
                join_type="inner",
                evidence="user_confirmed",
                meaning="按稳定发票编号关联凭证",
            )
        ],
    )
    facts = {item.id: item for item in contract.facts}

    def source_fact_draft(fact_id, meaning):
        def build(binding):
            return ReferenceFactDraft(
                fact_id=fact_id,
                meaning=meaning,
                actual_projection=[],
                plan=compile_fact_plan(
                    contract,
                    binding,
                    facts[fact_id],
                ),
                comparator=None,
                comparison_role="source_fact",
            )
        return build

    _validate(
        contract,
        proposal,
        [
            source_fact_draft(
                "invoice_revenue",
                "业务端发票收入事实",
            ),
            source_fact_draft(
                "journal_revenue",
                "财务端凭证收入事实",
            ),
        ],
        lambda binding: compile_contract_plan(contract, binding),
        result_comparator=ComparatorSpec(
            type="keyed_rows_equal",
            key_columns=["InvoiceId"],
            compare_columns=["InvoiceAmount", "JournalAmount"],
            tolerance={
                "InvoiceAmount": 0.01,
                "JournalAmount": 0.01,
            },
        ),
    )
