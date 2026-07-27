import pytest
from pydantic import ValidationError

from app.services.schema_resolution_v3 import make_binding_candidate, make_issue


def test_physical_ambiguity_requires_two_typed_candidates():
    candidate = make_binding_candidate(
        semantic_id="amount",
        business_label="候选金额",
        physical_binding_fragment={"object_id": 1, "column_id": 2},
    )
    with pytest.raises(ValidationError, match="至少两个"):
        make_issue(
            code="SCHEMA_PHYSICAL_BINDING_AMBIGUOUS",
            category="physical_ambiguity",
            semantic_id="amount",
            business_meaning="收入金额",
            reason="存在多个合理字段",
            required_semantic_shape="user_choice_required",
            physical_candidates=[candidate],
        )


def test_capability_gap_cannot_offer_a_physical_choice():
    candidate = make_binding_candidate(
        semantic_id="amount",
        business_label="候选金额",
        physical_binding_fragment={"object_id": 1, "column_id": 2},
    )
    with pytest.raises(ValidationError, match="只有物理歧义"):
        make_issue(
            code="SCHEMA_SEMANTIC_SHAPE_UNBINDABLE",
            category="semantic_capability_gap",
            semantic_id="amount",
            business_meaning="收入金额",
            reason="需要多个字段计算",
            required_semantic_shape="derived_expression",
            physical_candidates=[candidate],
        )


def test_candidate_identity_depends_on_binding_not_display_text():
    first = make_binding_candidate(
        semantic_id="amount",
        business_label="候选一",
        physical_binding_fragment={"object_id": 1, "column_id": 2},
    )
    second = make_binding_candidate(
        semantic_id="amount",
        business_label="另一种展示",
        physical_binding_fragment={"object_id": 1, "column_id": 2},
    )
    assert first.candidate_id == second.candidate_id

