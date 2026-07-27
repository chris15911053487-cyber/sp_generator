"""在不暴露 Reference SQL 的前提下生成和编译查询型 SP。"""

from __future__ import annotations

from collections.abc import Callable

from app.contracts.reference import ReferenceBundle
from app.contracts.relational_plan import RelationalPlan
from app.contracts.schema import SchemaBinding
from app.contracts.semantic import SemanticContract
from app.contracts.validation import ProcedureCandidateV3
from app.services.sql_compile_v3 import (
    CompileContractError,
    compile_procedure,
    parameter_definitions,
    validate_compiled_result_schema,
)
from app.services.sql_renderer_v3 import RENDERER_VERSION, SqlRendererV3
from app.services.plan_semantics_v3 import validate_plan_semantics


class ProcedureBuildError(ValueError):
    def __init__(self, code: str, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.code = code
        self.evidence = evidence or {}


PlanFactory = Callable[[SemanticContract, SchemaBinding], RelationalPlan]


def generate_procedure_candidate(
    contract: SemanticContract,
    binding: SchemaBinding,
    reference: ReferenceBundle,
    plan_factory: PlanFactory,
    *,
    compiler=None,
) -> ProcedureCandidateV3:
    """plan_factory 只接收语义合同和 SchemaBinding，不接收 Reference 制品。"""
    if reference.status != "reference_ready":
        raise ProcedureBuildError(
            "PROCEDURE_REFERENCE_NOT_READY",
            "Reference 未冻结，禁止生成 SP",
        )
    if reference.contract_hash != contract.content_hash:
        raise ProcedureBuildError(
            "PROCEDURE_CONTRACT_HASH_MISMATCH",
            "Reference 与当前语义合同不一致",
        )
    if reference.binding_hash != binding.content_hash:
        raise ProcedureBuildError(
            "PROCEDURE_BINDING_HASH_MISMATCH",
            "Reference 与当前 SchemaBinding 不一致",
        )
    if reference.renderer_version != RENDERER_VERSION:
        raise ProcedureBuildError(
            "PROCEDURE_RENDERER_VERSION_MISMATCH",
            "Reference 使用了不同版本的 SQL Renderer",
        )

    procedure_plan = plan_factory(contract, binding)
    if contract.facts:
        from app.services.fact_compiler_v3 import compile_contract_plan

        expected_plan = compile_contract_plan(contract, binding)
        if procedure_plan.canonical_json() != expected_plan.canonical_json():
            raise ProcedureBuildError(
                "PROCEDURE_PLAN_SEMANTIC_MISMATCH",
                "结构化事实合同的 SP 计划偏离冻结语义",
            )
    else:
        validate_plan_semantics(procedure_plan, contract, binding)
    renderer = SqlRendererV3(contract, binding)
    sql = renderer.render_procedure(procedure_plan)
    compiled = compile_procedure(contract.procedure_name, sql, contract, compiler)
    if not compiled.get("ok"):
        raise ProcedureBuildError(
            "PROCEDURE_COMPILE_FAILED",
            "SP 未通过 SQL Server 静态编译",
            evidence=compiled,
        )
    try:
        validate_compiled_result_schema(
            compiled,
            procedure_plan.result_schema,
            artifact="Procedure",
        )
    except CompileContractError as exc:
        raise ProcedureBuildError(
            "PROCEDURE_RESULT_SCHEMA_MISMATCH",
            str(exc),
            evidence=exc.evidence,
        ) from exc
    return ProcedureCandidateV3(
        contract_hash=contract.content_hash,
        binding_hash=binding.content_hash,
        reference_bundle_hash=reference.content_hash,
        renderer_version=RENDERER_VERSION,
        procedure_plan=procedure_plan,
        procedure_sql=sql,
        parameters=parameter_definitions(contract),
        result_schema=procedure_plan.result_schema,
        compile_evidence=compiled,
        safety_evidence={
            "query_only": True,
            "dynamic_sql": False,
            "rendered_from_restricted_plan": True,
        },
        status="candidate_compiled",
    )
