from app.contracts.semantic_design import ResultContract, ResultOutputSpec
from app.contracts.semantic_design_state import SemanticDesignDiagnostic
from app.services.semantic_design_checkpoints import (
    advance_semantic_design_checkpoint,
    new_semantic_design_checkpoint,
    record_semantic_design_failure,
)


def _result(name="usp_Test"):
    return ResultContract(
        procedure_name=name,
        purpose="测试结果契约",
        result_mode="full_rows",
        outputs=[
            ResultOutputSpec(
                symbol="document_number",
                name="DocumentNumber",
                meaning="单据编号",
                logical_type="string",
                nullable=False,
            ),
        ],
        grain_output_symbols=["document_number"],
    )


def test_result_change_invalidates_every_downstream_product():
    checkpoint = new_semantic_design_checkpoint("session-1", "d" * 64)
    checkpoint = advance_semantic_design_checkpoint(
        checkpoint, "result_contract", _result(),
    )
    populated = checkpoint.model_copy(update={
        "fact_blueprint": {"old": True},
        "computation_blueprint": {"old": True},
        "semantic_obligations": {"old": True},
        "semantic_inputs": {"old": True},
        "source_requirements": {"old": True},
        "expression_design": {"old": True},
        "compile_result": {"old": True},
    })

    changed = advance_semantic_design_checkpoint(
        populated, "result_contract", _result("usp_Changed"),
    )

    assert changed.fact_blueprint is None
    assert changed.computation_blueprint is None
    assert changed.semantic_obligations is None
    assert changed.semantic_inputs is None
    assert changed.source_requirements is None
    assert changed.expression_design is None
    assert changed.compile_result is None
    assert changed.stage == "fact_blueprint"


def test_same_result_keeps_frozen_downstream_products():
    checkpoint = new_semantic_design_checkpoint("session-1", "d" * 64)
    checkpoint = advance_semantic_design_checkpoint(
        checkpoint, "result_contract", _result(),
    )
    populated = checkpoint.model_copy(update={"compile_result": {"old": True}})

    unchanged = advance_semantic_design_checkpoint(
        populated, "result_contract", _result(),
    )

    assert unchanged.compile_result == {"old": True}


def test_only_one_local_repair_is_allowed():
    checkpoint = new_semantic_design_checkpoint("session-1", "d" * 64)
    diagnostic = SemanticDesignDiagnostic(
        stage="result_contract",
        code="RESULT_INVALID",
        message="结果契约无效",
        system_action="只重试结果契约阶段",
    )
    checkpoint = record_semantic_design_failure(checkpoint, diagnostic)
    assert checkpoint.repair_counts["result_contract"] == 1

    try:
        record_semantic_design_failure(checkpoint, diagnostic)
    except ValueError as exc:
        assert "SEMANTIC_DESIGN_REPAIR_LIMIT_EXCEEDED" in str(exc)
    else:
        raise AssertionError("同一阶段第二次修复失败后必须停止")
