"""Reference-first：先生成、编译、预执行并冻结独立标准答案定义。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.contracts.reference import (
    ComparatorSpec,
    ReferenceBundle,
    ReferenceFact,
    ValidationCase,
)
from app.contracts.relational_plan import (
    Expression,
    NamedExpression,
    PlanNode,
    RelationalPlan,
    ResultColumn,
)
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract
from app.services.sql_compile_v3 import (
    CompileContractError,
    compile_reference,
    validate_compiled_result_schema,
)
from app.services.sql_renderer_v3 import (
    RENDERER_VERSION,
    SqlRenderError,
    SqlRendererV3,
)
from app.services.plan_semantics_v3 import validate_plan_semantics


class ReferenceBuildError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


@dataclass(frozen=True)
class ReferenceFactDraft:
    fact_id: str
    meaning: str
    actual_projection: list[str]
    plan: RelationalPlan
    comparator: ComparatorSpec | None
    comparison_role: str = "direct_actual"


PreflightExecutor = Callable[[str, dict], list[dict]]


def _scan_entities(node: PlanNode) -> set[str]:
    result = {node.entity_id} if node.kind == "scan" and node.entity_id else set()
    for child in (node.input, node.left, node.right):
        if child is not None:
            result.update(_scan_entities(child))
    for child in node.inputs:
        result.update(_scan_entities(child))
    return result


def referenced_object_ids(
    plan: RelationalPlan,
    binding: SchemaBinding,
) -> list[int]:
    entities = {item.entity_id: item for item in binding.entities}
    try:
        return sorted(
            entities[entity_id].object_id
            for entity_id in _scan_entities(plan.root)
        )
    except KeyError as exc:
        raise ReferenceBuildError(
            "REFERENCE_PLAN_ENTITY_UNKNOWN",
            f"Reference 计划引用了未绑定实体 {exc.args[0]}",
        ) from exc


def _find_aggregate(node: PlanNode) -> PlanNode | None:
    if node.kind == "aggregate":
        return node
    if node.input is not None:
        found = _find_aggregate(node.input)
        if found is not None:
            return found
    return None


def coverage_plan(
    plan: RelationalPlan,
    binding: SchemaBinding,
) -> RelationalPlan | None:
    aggregate = _find_aggregate(plan.root)
    if aggregate is None or aggregate.input is None:
        return None
    entities = _scan_entities(aggregate.input)
    candidates = [
        item for item in binding.fields
        if item.entity_id in entities and not item.nullable
    ] or [
        item for item in binding.fields if item.entity_id in entities
    ]
    if not candidates:
        raise ReferenceBuildError(
            "REFERENCE_COVERAGE_FIELD_MISSING",
            "无法为聚合 Reference 构造源数据覆盖检查",
        )
    count_node = PlanNode(
        node_id="coverage_count",
        kind="aggregate",
        input=aggregate.input,
        aggregates=[
            NamedExpression(
                name="CoverageCount",
                expression=Expression(
                    kind="function",
                    operator="COUNT",
                    args=[
                        Expression(
                            kind="column",
                            field_binding_id=candidates[0].binding_id,
                        )
                    ],
                ),
            )
        ],
    )
    return RelationalPlan(
        plan_id=plan.plan_id + ":coverage",
        purpose="证明聚合结果命中了真实源数据",
        root=count_node,
        result_schema=[
            ResultColumn(
                name="CoverageCount",
                logical_type="integer",
                # SQL Server 的结果元数据把 COUNT 表达式标记为可空，
                # 即使运行时无匹配行也返回 0；计划必须忠实匹配编译元数据。
                nullable=True,
            )
        ],
    )


def _validate_case_parameters(
    contract: SemanticContract,
    validation_cases: list[ValidationCase],
) -> None:
    parameter_ids = {item.id for item in contract.parameters}
    required = {item.id for item in contract.parameters if item.required}
    for case in validation_cases:
        supplied = set(case.parameters)
        unknown = supplied - parameter_ids
        missing = required - supplied
        if unknown or missing:
            raise ReferenceBuildError(
                "REFERENCE_CASE_PARAMETER_INVALID",
                f"校验用例 {case.case_id} 的参数不符合语义合同",
                evidence={"unknown": sorted(unknown), "missing": sorted(missing)},
            )


def freeze_reference_bundle(
    contract: SemanticContract,
    binding: SchemaBinding,
    drafts: list[ReferenceFactDraft],
    validation_cases: list[ValidationCase],
    *,
    compiler=None,
    preflight_executor: PreflightExecutor,
    result_comparator: ComparatorSpec | None = None,
) -> ReferenceBundle:
    """只有全部 Reference 编译并在覆盖用例上执行成功时才返回可冻结制品。"""
    if not drafts:
        raise ReferenceBuildError("REFERENCE_FACT_MISSING", "至少需要一个 Reference Fact")
    _validate_case_parameters(contract, validation_cases)
    coverage = next(
        (item for item in validation_cases if item.kind == "coverage"),
        None,
    )
    if coverage is None:
        raise ReferenceBuildError(
            "REFERENCE_COVERAGE_CASE_MISSING",
            "Reference 必须包含覆盖用例",
        )

    renderer = SqlRendererV3(contract, binding)
    compile_evidence: dict[str, dict] = {}
    preflight_evidence: dict[str, dict] = {}
    facts: list[ReferenceFact] = []
    roles = {draft.comparison_role for draft in drafts}
    if not roles.issubset({"direct_actual", "source_fact"}) or len(roles) != 1:
        raise ReferenceBuildError(
            "REFERENCE_COMPARISON_ROLE_INVALID",
            "Reference Fact 必须统一使用 direct_actual 或 source_fact",
        )
    if roles == {"source_fact"}:
        expected_ids = {item.id for item in contract.facts}
        actual_ids = {item.fact_id for item in drafts}
        if actual_ids != expected_ids:
            raise ReferenceBuildError(
                "REFERENCE_FACT_SET_MISMATCH",
                "来源 Reference Fact 必须与冻结语义事实逐一对应",
                evidence={
                    "missing": sorted(expected_ids - actual_ids),
                    "extra": sorted(actual_ids - expected_ids),
                },
            )
    for draft in drafts:
        if draft.comparison_role == "direct_actual":
            validate_plan_semantics(
                draft.plan,
                contract,
                binding,
                output_projection=draft.actual_projection,
                allow_entity_subset=True,
            )
        else:
            from app.services.fact_compiler_v3 import compile_fact_plan

            semantic_fact = next(
                item for item in contract.facts
                if item.id == draft.fact_id
            )
            expected_plan = compile_fact_plan(
                contract, binding, semantic_fact,
            )
            if draft.plan.canonical_json() != expected_plan.canonical_json():
                raise ReferenceBuildError(
                    "REFERENCE_FACT_PLAN_MISMATCH",
                    f"Reference {draft.fact_id} 偏离冻结事实定义",
                )
        try:
            sql = renderer.render_query(draft.plan)
        except SqlRenderError as exc:
            raise ReferenceBuildError(
                exc.code,
                f"Reference {draft.fact_id} 无法安全渲染: {exc}",
                evidence={"plan_path": exc.plan_path},
            ) from exc
        compiled = compile_reference(sql, contract, compiler)
        compile_evidence[draft.fact_id] = compiled
        if not compiled.get("ok"):
            raise ReferenceBuildError(
                "REFERENCE_COMPILE_FAILED",
                f"Reference {draft.fact_id} 未通过 SQL Server 静态编译",
                evidence=compiled,
            )
        try:
            validate_compiled_result_schema(
                compiled,
                draft.plan.result_schema,
                artifact=f"Reference {draft.fact_id}",
            )
        except CompileContractError as exc:
            raise ReferenceBuildError(
                "REFERENCE_RESULT_SCHEMA_MISMATCH",
                str(exc),
                evidence=exc.evidence,
            ) from exc
        coverage_plan_value = coverage_plan(draft.plan, binding)
        coverage_sql = (
            renderer.render_query(coverage_plan_value)
            if coverage_plan_value is not None else None
        )
        if coverage_sql is not None:
            coverage_compiled = compile_reference(
                coverage_sql,
                contract,
                compiler,
            )
            compile_evidence[draft.fact_id + ":coverage"] = coverage_compiled
            if not coverage_compiled.get("ok"):
                raise ReferenceBuildError(
                    "REFERENCE_COVERAGE_COMPILE_FAILED",
                    f"Reference {draft.fact_id} 的覆盖查询未通过静态编译",
                    evidence=coverage_compiled,
                )
            try:
                validate_compiled_result_schema(
                    coverage_compiled,
                    coverage_plan_value.result_schema,
                    artifact=f"Reference {draft.fact_id} coverage",
                )
            except CompileContractError as exc:
                raise ReferenceBuildError(
                    "REFERENCE_COVERAGE_SCHEMA_MISMATCH",
                    str(exc),
                    evidence=exc.evidence,
                ) from exc

        rows = preflight_executor(sql, coverage.parameters)
        expected_names = [item.name.casefold() for item in draft.plan.result_schema]
        actual_names = (
            [str(item).casefold() for item in rows[0]]
            if rows else expected_names
        )
        if actual_names != expected_names:
            raise ReferenceBuildError(
                "REFERENCE_RESULT_SCHEMA_MISMATCH",
                f"Reference {draft.fact_id} 的运行时列与关系计划不一致",
                evidence={
                    "expected": expected_names,
                    "actual": actual_names,
                },
            )
        source_row_count = len(rows)
        if coverage_sql is not None:
            coverage_rows = preflight_executor(
                coverage_sql,
                coverage.parameters,
            )
            source_row_count = int(
                coverage_rows[0].get("CoverageCount", 0)
                if coverage_rows else 0
            )
        if (
            draft.comparison_role == "direct_actual"
            and (not rows or source_row_count <= 0)
        ):
            raise ReferenceBuildError(
                "REFERENCE_COVERAGE_EMPTY",
                f"覆盖用例未命中 Reference {draft.fact_id} 的任何业务数据",
                evidence={"case_id": coverage.case_id},
            )
        preflight_evidence[draft.fact_id] = {
            "case_id": coverage.case_id,
            "row_count": len(rows),
            "source_row_count": source_row_count,
            "executed": True,
        }
        object_ids = referenced_object_ids(draft.plan, binding)
        facts.append(
            ReferenceFact(
                fact_id=draft.fact_id,
                meaning=draft.meaning,
                comparison_role=draft.comparison_role,
                actual_projection=draft.actual_projection,
                reference_plan=draft.plan,
                expected_sql=sql,
                coverage_sql=coverage_sql,
                expected_schema=draft.plan.result_schema,
                comparator=draft.comparator,
                allowed_object_ids=object_ids,
            )
        )

    if roles == {"source_fact"} and not any(
        int(item.get("source_row_count", 0)) > 0
        for item in preflight_evidence.values()
    ):
        raise ReferenceBuildError(
            "REFERENCE_COVERAGE_EMPTY",
            "覆盖用例没有命中任何独立来源事实",
            evidence={"case_id": coverage.case_id},
        )

    return ReferenceBundle(
        contract_hash=contract.content_hash,
        binding_hash=binding.content_hash,
        renderer_version=RENDERER_VERSION,
        facts=facts,
        result_comparator=result_comparator,
        validation_cases=validation_cases,
        compile_evidence=compile_evidence,
        preflight_evidence=preflight_evidence,
        status="reference_ready",
    )
