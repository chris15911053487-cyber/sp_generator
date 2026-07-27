"""V3 结构化错误协议和流水线状态构造器。"""

from __future__ import annotations

import uuid

from app.contracts.validation import (
    GateResultV3,
    GateStage,
    IssueLocation,
    IssueV3,
)


GATE_ORDER: tuple[GateStage, ...] = (
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
)


def issue(
    *,
    code: str,
    stage: GateStage,
    artifact: str,
    title: str,
    summary: str,
    evidence: dict | None = None,
    technical_detail: str = "",
    retryable: bool = False,
    auto_fixable: bool = False,
    user_action: str = "请检查错误证据后修正定义。",
    status: str = "failed",
    location: IssueLocation | None = None,
    correlation_id: str | None = None,
) -> IssueV3:
    if location is None:
        if stage == "semantic_contract":
            location = IssueLocation(contract_path="$")
        elif stage in {
            "reference_plan", "procedure_plan", "result_contract",
            "business_comparison",
        }:
            location = IssueLocation(plan_path="root")
        else:
            location = IssueLocation()
    return IssueV3(
        issue_id=str(uuid.uuid4()),
        code=code,
        stage=stage,
        artifact=artifact,
        severity="error",
        status=status,
        title=title,
        summary=summary,
        evidence=evidence or {},
        location=location,
        retryable=retryable,
        auto_fixable=auto_fixable,
        user_action=user_action,
        technical_detail=technical_detail,
        correlation_id=correlation_id or str(uuid.uuid4()),
    )


def initial_gates() -> list[GateResultV3]:
    return [GateResultV3(stage=stage, status="not_run") for stage in GATE_ORDER]


class GatePipeline:
    """只允许单向推进；失败后其余步骤保持 not_run。"""

    def __init__(self):
        self._results = initial_gates()
        self._cursor = 0
        self._blocked = False

    def record(
        self,
        stage: GateStage,
        status: str,
        *,
        issues: list[IssueV3] | None = None,
        details: dict | None = None,
    ) -> None:
        if self._blocked:
            raise RuntimeError("流水线已经停止，不能继续执行后续 Gate")
        expected = GATE_ORDER[self._cursor]
        if stage != expected:
            raise RuntimeError(f"Gate 顺序错误：期望 {expected}，收到 {stage}")
        self._results[self._cursor] = GateResultV3(
            stage=stage,
            status=status,
            issues=issues or [],
            details=details or {},
        )
        self._cursor += 1
        if status in {"failed", "inconclusive"}:
            self._blocked = True

    def skip_prevalidated(
        self,
        through: GateStage,
        details: dict | None = None,
    ) -> None:
        target = GATE_ORDER.index(through)
        while self._cursor <= target:
            self.record(
                GATE_ORDER[self._cursor],
                "passed",
                details=details or {"source": "frozen_artifact"},
            )

    @property
    def results(self) -> list[GateResultV3]:
        return list(self._results)

    @property
    def blocked(self) -> bool:
        return self._blocked
