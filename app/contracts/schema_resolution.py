"""Contracts for resolving semantic designs against a physical catalog."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.schema import SchemaBindingProposal
from app.contracts.semantic import SemanticContract


ResolutionCategory = Literal[
    "binding_repairable",
    "physical_ambiguity",
    "semantic_capability_gap",
    "environment",
    "internal_generation",
]
RequiredSemanticShape = Literal[
    "direct_field",
    "derived_expression",
    "multi_entity_fact",
    "missing_join",
    "literal_mapping",
    "user_choice_required",
]
AllowedResolutionAction = Literal[
    "auto_repair",
    "user_select",
    "revise_semantic_shape",
    "retry_environment",
    "stop",
]


class SchemaBindingCandidate(StrictContract):
    candidate_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    business_label: str = Field(min_length=1)
    physical_binding_fragment: dict[str, Any]
    evidence: dict[str, Any] = Field(default_factory=dict)
    consequences: list[str] = Field(default_factory=list)


class SchemaResolutionIssue(StrictContract):
    issue_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    category: ResolutionCategory
    semantic_id: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_]*$",
    )
    business_meaning: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    catalog_evidence: dict[str, Any] = Field(default_factory=dict)
    required_semantic_shape: RequiredSemanticShape
    physical_candidates: list[SchemaBindingCandidate] = Field(
        default_factory=list,
    )
    allowed_action: AllowedResolutionAction

    @model_validator(mode="after")
    def validate_route(self):
        expected = {
            "binding_repairable": "auto_repair",
            "physical_ambiguity": "user_select",
            "semantic_capability_gap": "revise_semantic_shape",
            "environment": "retry_environment",
            "internal_generation": "stop",
        }[self.category]
        if self.allowed_action != expected:
            raise ValueError(
                f"{self.category} 只能使用动作 {expected}"
            )
        if self.category == "physical_ambiguity":
            if len(self.physical_candidates) < 2:
                raise ValueError("物理歧义必须提供至少两个结构化候选")
            if self.required_semantic_shape != "user_choice_required":
                raise ValueError("物理歧义必须标记 user_choice_required")
        elif self.physical_candidates:
            raise ValueError("只有物理歧义可以携带用户可选候选")
        if (
            self.category == "semantic_capability_gap"
            and self.required_semantic_shape
            in {"direct_field", "user_choice_required"}
        ):
            raise ValueError("语义能力缺口必须声明需要改变的实现形状")
        return self


class SchemaResolutionCheckpoint(StrictContract):
    version: Literal[1] = 1
    checkpoint_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    design_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    partial_proposal: SchemaBindingProposal | None = None
    issues: list[SchemaResolutionIssue] = Field(default_factory=list)
    user_selections: dict[str, str] = Field(default_factory=dict)
    revision_count: int = Field(default=0, ge=0, le=2)
    repair_count: int = Field(default=0, ge=0, le=1)
    status: Literal[
        "proposing",
        "awaiting_schema_choice",
        "awaiting_design_reconfirmation",
        "resolved",
        "failed",
        "invalidated",
    ]

    @model_validator(mode="after")
    def validate_checkpoint_state(self):
        issue_ids = {item.issue_id for item in self.issues}
        if not set(self.user_selections).issubset(issue_ids):
            raise ValueError("Schema 选择引用未知问题")
        if self.status == "awaiting_schema_choice":
            selectable = {
                item.issue_id: {
                    candidate.candidate_id
                    for candidate in item.physical_candidates
                }
                for item in self.issues
                if item.category == "physical_ambiguity"
            }
            if not selectable:
                raise ValueError("等待 Schema 选择时必须存在物理歧义")
            for issue_id, candidate_id in self.user_selections.items():
                if (
                    issue_id in selectable
                    and candidate_id not in selectable[issue_id]
                ):
                    raise ValueError("Schema 选择不属于当前问题候选")
        if self.status == "resolved" and self.issues:
            raise ValueError("已解决 checkpoint 不能保留未决问题")
        return self


class SemanticRevisionProposal(StrictContract):
    base_contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revised_contract: SemanticContract
    addressed_issue_ids: list[str] = Field(min_length=1)
    change_summary: list[dict[str, Any]] = Field(default_factory=list)


class SemanticRevisionDiff(StrictContract):
    allowed: bool
    allowed_changes: list[dict[str, Any]] = Field(default_factory=list)
    forbidden_changes: list[dict[str, Any]] = Field(default_factory=list)
    unresolved_issue_ids: list[str] = Field(default_factory=list)

