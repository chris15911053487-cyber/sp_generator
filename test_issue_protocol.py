import pytest

from app.services.issues_v3 import GatePipeline, issue


def test_failure_keeps_later_gates_not_run():
    pipeline = GatePipeline()
    pipeline.record("environment", "passed")
    pipeline.record(
        "semantic_contract",
        "failed",
        issues=[
            issue(
                code="CONTRACT_INVALID",
                stage="semantic_contract",
                artifact="contract",
                title="合同错误",
                summary="缺少业务字段",
            )
        ],
    )
    statuses = {item.stage: item.status for item in pipeline.results}
    assert statuses["semantic_contract"] == "failed"
    assert statuses["schema_binding"] == "not_run"
    with pytest.raises(RuntimeError, match="已经停止"):
        pipeline.record("schema_binding", "passed")
