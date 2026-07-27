"""V3 gate, issue, candidate and evidence contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.contracts.base import StrictContract
from app.contracts.relational_plan import RelationalPlan, ResultColumn


GateStage = Literal[
    "environment",
    "semantic_contract",
    "schema_binding",
    "reference_plan",
    "reference_compile",
    "reference_preflight",
    "procedure_plan",
    "procedure_compile",
    "result_contract",
    "business_comparison",
    "evidence_integrity",
]
GateStatus = Literal["running", "passed", "failed", "not_run", "inconclusive"]


class IssueLocation(StrictContract):
    contract_path: str | None = None
    plan_path: str | None = None
    sql_line: int | None = Field(default=None, gt=0)


class IssueV3(StrictContract):
    issue_id: str = Field(min_length=1)
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    stage: GateStage
    artifact: str = Field(min_length=1)
    severity: Literal["error", "warning", "info"]
    status: Literal["failed", "inconclusive"]
    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    evidence: dict = Field(default_factory=dict)
    location: IssueLocation = Field(default_factory=IssueLocation)
    retryable: bool
    auto_fixable: bool
    user_action: str = Field(min_length=1)
    technical_detail: str = ""
    correlation_id: str = Field(min_length=1)


class GateResultV3(StrictContract):
    stage: GateStage
    status: GateStatus = "not_run"
    issues: list[IssueV3] = Field(default_factory=list)
    details: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status(self):
        if self.status in {"failed", "inconclusive"} and not self.issues:
            raise ValueError(f"{self.stage} {self.status} 必须包含 issue")
        if self.status == "passed" and any(
            item.severity == "error" for item in self.issues
        ):
            raise ValueError(f"{self.stage} passed 不能包含 error")
        return self


class ProcedureCandidateV3(StrictContract):
    version: Literal[3] = 3
    contract_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    renderer_version: str = Field(min_length=1)
    procedure_plan: RelationalPlan
    procedure_sql: str = Field(min_length=1)
    parameters: list[dict]
    result_schema: list[ResultColumn]
    compile_evidence: dict = Field(default_factory=dict)
    safety_evidence: dict = Field(default_factory=dict)
    status: Literal["candidate_generated", "candidate_compiled"]

    @model_validator(mode="after")
    def validate_candidate(self):
        if self.result_schema != self.procedure_plan.result_schema:
            raise ValueError("ProcedureCandidate 结果结构与关系计划不一致")
        if self.status == "candidate_compiled" and not self.compile_evidence.get("ok"):
            raise ValueError("candidate_compiled 必须包含成功编译证据")
        if not self.safety_evidence.get("rendered_from_restricted_plan"):
            raise ValueError("ProcedureCandidate 缺少受限计划渲染证据")
        return self


class ComparisonEvidence(StrictContract):
    fact_id: str
    comparator: str
    match: bool
    actual_row_count: int = Field(ge=0)
    expected_row_count: int = Field(ge=0)
    missing: list[dict] = Field(default_factory=list)
    extra: list[dict] = Field(default_factory=list)
    duplicate_keys: list[dict] = Field(default_factory=list)
    differences: list[dict] = Field(default_factory=list)
    difference_totals: dict[str, int] = Field(default_factory=dict)
    summary: str


class CoverageEvidence(StrictContract):
    effective: bool
    expected_row_count: int = Field(ge=0)
    actual_row_count: int = Field(ge=0)
    case_id: str


class ValidationEvidence(StrictContract):
    version: Literal[3] = 3
    candidate_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reference_bundle_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    catalog_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_identity: str
    validation_case: dict
    stages: list[GateResultV3]
    comparisons: list[ComparisonEvidence] = Field(default_factory=list)
    coverage: CoverageEvidence | None = None
    status: Literal["validated", "needs_review", "failed", "inconclusive"]
    created_at: datetime

    @model_validator(mode="after")
    def validate_pipeline(self):
        order = [
            "environment",
            "semantic_contract",
            "schema_binding",
            "reference_plan",
            "reference_compile",
            "reference_preflight",
            "procedure_plan",
            "procedure_compile",
            "result_contract",
            "business_comparison",
            "evidence_integrity",
        ]
        by_stage = {item.stage: item for item in self.stages}
        if len(by_stage) != len(self.stages):
            raise ValueError("ValidationEvidence 存在重复 gate")
        blocked = False
        for stage in order:
            item = by_stage.get(stage)
            if item is None:
                raise ValueError(f"ValidationEvidence 缺少 gate: {stage}")
            if blocked and item.status != "not_run":
                raise ValueError(f"{stage} 应为 not_run")
            if item.status in {"failed", "inconclusive"}:
                blocked = True
        if self.status == "validated":
            if any(item.status != "passed" for item in self.stages):
                raise ValueError("validated 要求全部 gate passed")
            case_kind = self.validation_case.get("kind")
            if self.coverage is None:
                raise ValueError("validated 要求覆盖证据")
            if case_kind != "empty" and not self.coverage.effective:
                raise ValueError("validated 要求有效数据覆盖")
            if case_kind == "empty" and (
                self.coverage.expected_row_count != 0
                or self.coverage.actual_row_count != 0
            ):
                raise ValueError("validated 的空结果用例必须双方均为空")
            if any(not item.match for item in self.comparisons):
                raise ValueError("validated 不能包含不一致比较")
        if self.status == "failed" and not any(
            item.status == "failed" for item in self.stages
        ):
            raise ValueError("failed 必须包含失败 Gate")
        if self.status == "inconclusive" and not any(
            item.status == "inconclusive" for item in self.stages
        ):
            raise ValueError("inconclusive 必须包含无法判定 Gate")
        return self
