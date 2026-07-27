import pytest

from app.contracts.semantic import SemanticContract
from app.agent.nodes import (
    _canonicalize_full_day_boundaries,
    _canonicalize_explicit_output_names,
    _explicit_output_count,
    _explicit_output_names,
    _validate_assumption_semantics,
    _validate_explicit_output_count,
)
from app.contracts.semantic import SemanticDesign
from app.services.decision_contract import DecisionPlan, freeze_decisions


def test_decision_plan_rejects_physical_schema_choices():
    with pytest.raises(ValueError, match="物理对象引用"):
        DecisionPlan.model_validate({
            "requirements_summary": "查询应收发票",
            "decisions": [{
                "decision_key": "tax_source",
                "decision_type": "defaultable",
                "question": "税额使用哪个字段？",
                "options": [
                    {"id": "A", "value": "使用 OINV.VatSum"},
                    {"id": "B", "value": "使用其他字段"},
                ],
                "recommended_option_id": "A",
            }],
        })


def test_semantic_contract_rejects_physical_names_in_meanings():
    with pytest.raises(ValueError, match="疑似物理字段或表名"):
        SemanticContract.model_validate({
            "version": 3,
            "contract_id": "invoice-detail",
            "procedure_name": "GetInvoiceDetail",
            "purpose": "查询应收发票",
            "result_mode": "full_rows",
            "entities": [{"id": "invoice", "meaning": "OINV 应收发票"}],
            "grain": ["invoice_id"],
            "outputs": [{
                "id": "invoice_id",
                "name": "InvoiceId",
                "meaning": "发票标识",
                "logical_type": "integer",
                "nullable": False,
            }],
        })


def test_full_day_boundaries_are_canonicalized_deterministically():
    draft = {
        "contracts": [{
            "parameters": [
                {"id": "from_date", "boundary": "inclusive_full_day"},
                {"id": "to_date", "boundary": "inclusive"},
            ],
            "filters": [{
                "operator": "full_day_range",
                "parameter_ids": ["from_date", "to_date"],
            }],
        }],
    }

    _canonicalize_full_day_boundaries(draft)

    assert draft["contracts"][0]["parameters"] == [
        {"id": "from_date", "boundary": "inclusive"},
        {"id": "to_date", "boundary": "inclusive_full_day"},
    ]


def test_decision_hash_includes_requirements_summary():
    first = DecisionPlan(
        requirements_summary="查询应收发票",
        decisions=[],
    )
    second = DecisionPlan(
        requirements_summary="查询销售订单",
        decisions=[],
    )

    assert freeze_decisions(first).decision_hash != freeze_decisions(second).decision_hash


def test_semantic_contract_rejects_duplicate_business_outputs():
    with pytest.raises(ValueError, match="输出存在语义重复"):
        SemanticContract.model_validate({
            "version": 3,
            "contract_id": "invoice-detail",
            "procedure_name": "GetInvoiceDetail",
            "purpose": "查询应收发票",
            "result_mode": "full_rows",
            "entities": [{"id": "invoice", "meaning": "应收发票"}],
            "grain": ["invoice_id"],
            "outputs": [
                {
                    "id": "invoice_id",
                    "name": "ARInvoiceId",
                    "meaning": "应收发票内部标识",
                    "logical_type": "integer",
                    "nullable": False,
                },
                {
                    "id": "invoice_internal_id",
                    "name": "InvoiceInternalId",
                    "meaning": "发票内部编号",
                    "logical_type": "integer",
                    "nullable": False,
                },
            ],
        })


def test_semantic_contract_does_not_confuse_tax_and_untaxed_amounts():
    contract = SemanticContract.model_validate({
        "version": 3,
        "contract_id": "invoice-detail",
        "procedure_name": "GetInvoiceDetail",
        "purpose": "查询应收发票",
        "result_mode": "full_rows",
        "entities": [{"id": "invoice", "meaning": "应收发票"}],
        "grain": ["invoice_id"],
        "outputs": [
            {
                "id": "invoice_id",
                "name": "InvoiceId",
                "meaning": "发票内部编号",
                "logical_type": "integer",
                "nullable": False,
            },
            {
                "id": "untaxed_amount",
                "name": "UntaxedAmount",
                "meaning": "未税金额（单据币种，原始值）",
                "logical_type": "money",
                "nullable": False,
            },
            {
                "id": "tax_amount",
                "name": "TaxAmount",
                "meaning": "税额（单据币种，原始值）",
                "logical_type": "money",
                "nullable": False,
            },
        ],
    })

    assert len(contract.outputs) == 3


def test_semantic_contract_does_not_confuse_tax_and_gross_amounts():
    contract = SemanticContract.model_validate({
        "version": 3,
        "contract_id": "invoice-detail",
        "procedure_name": "GetInvoiceDetail",
        "purpose": "查询应收发票",
        "result_mode": "full_rows",
        "entities": [{"id": "invoice", "meaning": "应收发票"}],
        "grain": ["invoice_id"],
        "outputs": [
            {
                "id": "invoice_id",
                "name": "InvoiceId",
                "meaning": "发票内部编号",
                "logical_type": "integer",
                "nullable": False,
            },
            {
                "id": "tax_amount",
                "name": "TaxAmount",
                "meaning": "税额，取自发票记录",
                "logical_type": "money",
                "nullable": False,
            },
            {
                "id": "gross_amount",
                "name": "GrossAmount",
                "meaning": "含税总额，取自发票记录",
                "logical_type": "money",
                "nullable": False,
            },
        ],
    })

    assert len(contract.outputs) == 3


def test_assumption_semantics_reject_physical_field_names():
    with pytest.raises(ValueError, match="DocStatus"):
        _validate_assumption_semantics([{
            "key": "cancelled",
            "title": "如何判断取消单据？",
            "value": "使用 DocStatus=C",
            "reason": "确保过滤正确",
        }])


def test_semantic_contract_rejects_known_sap_physical_output_alias():
    with pytest.raises(ValueError, match="SAP B1 物理字段名: DocEntry"):
        SemanticContract.model_validate({
            "version": 3,
            "contract_id": "invoice-detail",
            "procedure_name": "GetInvoiceDetail",
            "purpose": "查询应收发票",
            "result_mode": "full_rows",
            "entities": [{"id": "invoice", "meaning": "应收发票"}],
            "grain": ["invoice_id"],
            "outputs": [{
                "id": "invoice_id",
                "name": "DocEntry",
                "meaning": "发票内部编号",
                "logical_type": "integer",
                "nullable": False,
            }],
        })


def test_explicit_output_count_is_enforced():
    assert _explicit_output_count("只允许返回8列：编号、日期等") == 8
    design = SemanticDesign.model_validate({
        "version": 3,
        "design_version": "design-1",
        "decision_hash": "decision-1",
        "contracts": [{
            "version": 3,
            "contract_id": "invoice-detail",
            "procedure_name": "GetInvoiceDetail",
            "purpose": "查询应收发票",
            "result_mode": "full_rows",
            "entities": [{"id": "invoice", "meaning": "应收发票"}],
            "grain": ["invoice_id"],
            "outputs": [{
                "id": "invoice_id",
                "name": "InvoiceId",
                "meaning": "发票内部编号",
                "logical_type": "integer",
                "nullable": False,
            }],
        }],
    })

    with pytest.raises(ValueError, match="明确要求 8 个输出"):
        _validate_explicit_output_count(design, "只返回8列")


def test_explicit_business_output_names_override_model_aliases():
    requirements = (
        "只返回3列：InvoiceInternalId, DocumentNumber, GrossAmount。"
    )
    draft = {"contracts": [{"outputs": [
        {"name": "DocEntry"},
        {"name": "DocNum"},
        {"name": "DocTotal"},
    ]}]}

    assert _explicit_output_names(requirements) == [
        "InvoiceInternalId", "DocumentNumber", "GrossAmount",
    ]
    _canonicalize_explicit_output_names(draft, requirements)

    assert [item["name"] for item in draft["contracts"][0]["outputs"]] == [
        "InvoiceInternalId", "DocumentNumber", "GrossAmount",
    ]
