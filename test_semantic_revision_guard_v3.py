from copy import deepcopy

from app.contracts.schema_resolution import SemanticRevisionProposal
from app.contracts.semantic import SemanticContract, SemanticSourceField
from app.services.schema_resolution_v3 import make_issue
from app.services.semantic_revision_v3 import evaluate_semantic_revision
from v3_test_helpers import contract


def _issue():
    return make_issue(
        code="SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
        category="semantic_capability_gap",
        semantic_id="amount",
        business_meaning="收入金额",
        reason="需要表达式",
        required_semantic_shape="derived_expression",
    )


def test_revision_rejects_business_output_change():
    base = contract()
    payload = base.model_dump(mode="python", by_alias=True)
    payload["outputs"][1]["meaning"] = "另一种收入口径"
    revised = SemanticContract.model_validate(payload)
    issue = _issue()
    result = evaluate_semantic_revision(
        base,
        SemanticRevisionProposal(
            base_contract_hash=base.content_hash,
            revised_contract=revised,
            addressed_issue_ids=[issue.issue_id],
        ),
        [issue],
    )
    assert not result.allowed
    assert any(item["path"] == "outputs" for item in result.forbidden_changes)


def test_revision_allows_used_source_field_implementation_change():
    base = contract()
    payload = base.model_dump(mode="python", by_alias=True)
    payload["source_fields"] = [
        SemanticSourceField(
            id="invoice_date",
            entity_id="invoice",
            meaning="发票日期",
            logical_type="date",
            nullable=False,
        ).model_dump(mode="python"),
    ]
    # The existing date filter uses this source field, so it is not speculative.
    revised = SemanticContract.model_validate(payload)
    issue = _issue()
    result = evaluate_semantic_revision(
        base,
        SemanticRevisionProposal(
            base_contract_hash=base.content_hash,
            revised_contract=revised,
            addressed_issue_ids=[issue.issue_id],
        ),
        [issue],
    )
    assert result.allowed
    assert {item["path"] for item in result.allowed_changes} == {
        "source_fields"
    }


def test_revision_rejects_unreferenced_new_source_field():
    base = contract()
    payload = deepcopy(base.model_dump(mode="python", by_alias=True))
    payload["source_fields"] = [{
        "id": "unused_amount_part",
        "entity_id": "invoice",
        "meaning": "未被使用的金额组成",
        "logical_type": "money",
        "nullable": False,
    }]
    revised = SemanticContract.model_validate(payload)
    issue = _issue()
    result = evaluate_semantic_revision(
        base,
        SemanticRevisionProposal(
            base_contract_hash=base.content_hash,
            revised_contract=revised,
            addressed_issue_ids=[issue.issue_id],
        ),
        [issue],
    )
    assert not result.allowed
    assert any(
        item.get("reason") == "新增源字段未被过滤或事实引用"
        for item in result.forbidden_changes
    )
