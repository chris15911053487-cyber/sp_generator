"""真实测试库 E2E：应收发票明细，只生成临时过程并回滚。"""

import json
import os

from app.agent.nodes import _generate_node_v3, _get_llm, _verify_node_v3
from app.contracts.semantic import SemanticDesign
from app.db.sqlite import create_session
from config import get_db_config, is_explicit_test_database


def _design() -> SemanticDesign:
    return SemanticDesign.model_validate({
        "version": 3,
        "design_version": "e2e-ar-invoice-v1",
        "decision_hash": "e2e-ar-invoice-v1",
        "contracts": [{
            "version": 3,
            "contract_id": "e2e-ar-invoice-v1:usp_GetARInvoiceDetail",
            "procedure_name": "usp_GetARInvoiceDetail",
            "purpose": "按必填起止过账日期查询标准应收发票明细，每张发票一行，排除已取消发票",
            "result_mode": "full_rows",
            "allow_empty": True,
            "money_tolerance": 0.01,
            "parameters": [
                {
                    "id": "from_date",
                    "name": "@FromDate",
                    "logical_type": "date",
                    "required": True,
                    "default": None,
                    "meaning": "起始过账日期",
                    "boundary": "inclusive",
                },
                {
                    "id": "to_date",
                    "name": "@ToDate",
                    "logical_type": "date",
                    "required": True,
                    "default": None,
                    "meaning": "结束过账日期的完整自然日",
                    "boundary": "inclusive_full_day",
                },
            ],
            "entities": [{
                "id": "ar_invoice",
                "meaning": "标准应收发票",
            }],
            "grain": ["invoice_internal_id"],
            "outputs": [
                {
                    "id": "invoice_internal_id",
                    "name": "InvoiceInternalId",
                    "meaning": "发票内部编号",
                    "logical_type": "integer",
                    "nullable": False,
                },
                {
                    "id": "document_number",
                    "name": "DocumentNumber",
                    "meaning": "单据编号",
                    "logical_type": "integer",
                    "nullable": True,
                },
                {
                    "id": "customer_code",
                    "name": "CustomerCode",
                    "meaning": "客户编码",
                    "logical_type": "string",
                    "nullable": True,
                },
                {
                    "id": "customer_name",
                    "name": "CustomerName",
                    "meaning": "客户名称",
                    "logical_type": "string",
                    "nullable": True,
                },
                {
                    "id": "posting_date",
                    "name": "PostingDate",
                    "meaning": "过账日期",
                    "logical_type": "date",
                    "nullable": True,
                },
                {
                    "id": "net_amount",
                    "name": "NetAmount",
                    "meaning": "未税金额，按账套系统币（SC）",
                    "logical_type": "money",
                    "nullable": True,
                },
                {
                    "id": "tax_amount",
                    "name": "TaxAmount",
                    "meaning": "税额，取发票合计税额并按账套系统币（SC）",
                    "logical_type": "money",
                    "nullable": True,
                },
                {
                    "id": "gross_amount",
                    "name": "GrossAmount",
                    "meaning": "含税总额，取发票合计金额并按账套系统币（SC）",
                    "logical_type": "money",
                    "nullable": True,
                },
            ],
            "filters": [
                {
                    "id": "posting_date_range",
                    "meaning": "过账日期在完整自然日范围内",
                    "field_ids": ["posting_date"],
                    "parameter_ids": ["from_date", "to_date"],
                    "operator": "full_day_range",
                    "literal_values": [],
                },
                {
                    "id": "exclude_cancelled",
                    "meaning": "仅返回未取消发票",
                    "field_ids": ["cancellation_status"],
                    "parameter_ids": [],
                    "operator": "eq",
                    "literal_values": ["not_cancelled"],
                },
            ],
            "derived_fields": [{
                "output_id": "net_amount",
                "expression": {
                    "kind": "binary",
                    "operator": "-",
                    "args": [
                        {"kind": "output", "output_id": "gross_amount"},
                        {"kind": "output", "output_id": "tax_amount"},
                    ],
                },
            }],
        }],
    })


def _writer(event: dict) -> None:
    summary = {
        key: value
        for key, value in event.items()
        if key in {"stage", "status", "message", "error", "attempt", "procedure_name"}
    }
    print("PROGRESS " + json.dumps(summary, ensure_ascii=False), flush=True)


def _sp_summary(sp_list: list[dict] | None) -> list[dict]:
    return [
        {
            "name": item.get("name"),
            "code_chars": len(
                item.get("code") or item.get("procedure_sql") or ""
            ),
            "verify_query_count": len(item.get("verify_queries") or []),
        }
        for item in (sp_list or [])
    ]


def _verify_summary(results: list[dict] | None) -> list[dict]:
    summaries = []
    for result in results or []:
        stage_results = result.get("stages") or result.get("stage_results") or []
        issues = result.get("issues") or [
            issue
            for stage in stage_results
            for issue in (stage.get("issues") or [])
        ]
        summaries.append({
            "name": result.get("name") or result.get("sp_name"),
            "status": result.get("status"),
            "deployment_eligible": result.get("deployment_eligible"),
            "stages": [
                {
                    "stage": stage.get("stage"),
                    "status": stage.get("status"),
                }
                for stage in stage_results
            ],
            "issues": [
                {
                    "stage": issue.get("stage"),
                    "code": issue.get("code"),
                    "status": issue.get("status"),
                    "message": issue.get("message") or issue.get("summary"),
                }
                for issue in issues[:12]
            ],
        })
    return summaries


def main() -> int:
    if os.getenv("RUN_V3_E2E") != "1":
        raise RuntimeError("必须显式设置 RUN_V3_E2E=1")
    db_config = get_db_config()
    if not is_explicit_test_database(db_config):
        raise RuntimeError("只允许 environment=test 的明确测试数据库")

    session = create_session("E2E-direct-frozen-semantic")
    state = {"session_id": session["id"]}
    generated = _generate_node_v3(
        state,
        _design(),
        _get_llm(),
        _writer,
    )
    print("GENERATE_STATUS " + json.dumps({
        "status": generated.get("status"),
        "error": generated.get("error"),
        "sp_list": _sp_summary(generated.get("sp_list")),
    }, ensure_ascii=False), flush=True)
    if generated.get("status") != "candidate_generated":
        return 1

    state.update(generated)
    verified = _verify_node_v3(state, _writer)
    print("VERIFY_STATUS " + json.dumps({
        "status": verified.get("status"),
        "error": verified.get("error"),
        "verify_results": _verify_summary(verified.get("verify_results")),
    }, ensure_ascii=False, default=str), flush=True)
    return 0 if verified.get("status") == "persisted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
