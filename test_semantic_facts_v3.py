import pytest

from app.contracts.semantic import SemanticContract


def reconciliation_payload():
    fact_ref = lambda fact, value: {
        "kind": "fact_value",
        "fact_value": {"fact_id": fact, "value_id": value},
    }
    return {
        "contract_id": "sales_vs_journal",
        "procedure_name": "usp_SalesVsJournal",
        "purpose": "按期间对比销售收入与财务凭证收入",
        "result_mode": "full_rows",
        "entities": [
            {"id": "sales", "meaning": "销售收入业务事实"},
            {"id": "journal", "meaning": "财务凭证收入事实"},
        ],
        "source_fields": [
            {
                "id": "sales_period",
                "entity_id": "sales",
                "meaning": "销售收入所属期间",
                "logical_type": "string",
            },
            {
                "id": "sales_amount",
                "entity_id": "sales",
                "meaning": "销售收入系统币金额",
                "logical_type": "money",
            },
            {
                "id": "journal_period",
                "entity_id": "journal",
                "meaning": "凭证收入所属期间",
                "logical_type": "string",
            },
            {
                "id": "journal_amount",
                "entity_id": "journal",
                "meaning": "凭证收入系统币金额",
                "logical_type": "money",
            },
        ],
        "outputs": [
            {
                "id": "period",
                "name": "Period",
                "meaning": "对账期间",
                "logical_type": "string",
            },
            {
                "id": "sales_revenue",
                "name": "SalesRevenue",
                "meaning": "销售侧收入系统币合计",
                "logical_type": "money",
            },
            {
                "id": "journal_revenue",
                "name": "JournalRevenue",
                "meaning": "凭证侧收入系统币合计",
                "logical_type": "money",
            },
            {
                "id": "difference",
                "name": "Difference",
                "meaning": "销售收入与凭证收入系统币差额",
                "logical_type": "money",
            },
        ],
        "grain": ["period"],
        "facts": [
            {
                "id": "sales_fact",
                "meaning": "按期间汇总销售收入",
                "entity_ids": ["sales"],
                "dimensions": [{
                    "id": "period",
                    "field_id": "sales_period",
                    "meaning": "销售期间",
                }],
                "measures": [{
                    "id": "revenue",
                    "field_id": "sales_amount",
                    "meaning": "销售收入合计",
                    "aggregation": "sum",
                    "logical_type": "money",
                }],
                "grain": ["period"],
            },
            {
                "id": "journal_fact",
                "meaning": "按期间汇总凭证收入",
                "entity_ids": ["journal"],
                "dimensions": [{
                    "id": "period",
                    "field_id": "journal_period",
                    "meaning": "凭证期间",
                }],
                "measures": [{
                    "id": "revenue",
                    "field_id": "journal_amount",
                    "meaning": "凭证收入合计",
                    "aggregation": "sum",
                    "logical_type": "money",
                }],
                "grain": ["period"],
            },
        ],
        "fact_joins": [{
            "id": "match_period",
            "keys": [{
                "left": {"fact_id": "sales_fact", "value_id": "period"},
                "right": {"fact_id": "journal_fact", "value_id": "period"},
            }],
            "join_type": "full",
            "meaning": "销售与凭证按期间匹配",
        }],
        "result_bindings": [
            {
                "output_id": "period",
                "expression": {
                    "kind": "function",
                    "operator": "COALESCE",
                    "args": [
                        fact_ref("sales_fact", "period"),
                        fact_ref("journal_fact", "period"),
                    ],
                },
            },
            {
                "output_id": "sales_revenue",
                "expression": fact_ref("sales_fact", "revenue"),
            },
            {
                "output_id": "journal_revenue",
                "expression": fact_ref("journal_fact", "revenue"),
            },
            {
                "output_id": "difference",
                "expression": {
                    "kind": "binary",
                    "operator": "-",
                    "args": [
                        fact_ref("sales_fact", "revenue"),
                        fact_ref("journal_fact", "revenue"),
                    ],
                },
            },
        ],
    }


def test_multi_source_reconciliation_is_fully_structured():
    value = SemanticContract.model_validate(reconciliation_payload())
    assert len(value.facts) == 2
    assert value.fact_joins[0].join_type == "full"


def test_multi_entity_contract_cannot_defer_fact_logic_to_llm_plan():
    payload = reconciliation_payload()
    payload["facts"] = []
    payload["fact_joins"] = []
    payload["result_bindings"] = []
    with pytest.raises(ValueError, match="必须声明结构化 facts"):
        SemanticContract.model_validate(payload)


def test_unknown_fact_value_reference_is_rejected():
    payload = reconciliation_payload()
    payload["result_bindings"][1]["expression"]["fact_value"][
        "value_id"
    ] = "invented_measure"
    with pytest.raises(ValueError, match="未知事实值引用"):
        SemanticContract.model_validate(payload)


def test_filter_cannot_compare_an_optional_null_parameter():
    payload = reconciliation_payload()
    payload["parameters"] = [{
        "id": "account_from",
        "name": "@AccountFrom",
        "logical_type": "string",
        "required": False,
        "default": None,
        "meaning": "收入科目范围起点",
        "boundary": "none",
    }]
    payload["filters"] = [{
        "id": "account_filter",
        "meaning": "筛选收入科目",
        "field_ids": ["journal_period"],
        "parameter_ids": ["account_from"],
        "operator": "gte",
    }]
    payload["facts"][1]["filter_ids"] = ["account_filter"]

    with pytest.raises(ValueError, match="无默认值的可选参数"):
        SemanticContract.model_validate(payload)


def test_optional_null_parameter_filter_can_declare_bypass_semantics():
    payload = reconciliation_payload()
    payload["parameters"] = [{
        "id": "account_from",
        "name": "@AccountFrom",
        "logical_type": "string",
        "required": False,
        "default": None,
        "meaning": "可选收入科目",
        "boundary": "none",
    }]
    payload["filters"] = [{
        "id": "account_filter",
        "meaning": "传入科目时筛选收入科目，否则不过滤",
        "field_ids": ["journal_period"],
        "parameter_ids": ["account_from"],
        "operator": "eq",
        "skip_when_parameter_null": True,
    }]
    payload["facts"][1]["filter_ids"] = ["account_filter"]

    value = SemanticContract.model_validate(payload)

    assert value.filters[0].skip_when_parameter_null is True


def test_direct_date_dimension_cannot_feed_a_string_result():
    payload = reconciliation_payload()
    for item in payload["source_fields"]:
        if item["id"] in {"sales_period", "journal_period"}:
            item["logical_type"] = "date"

    with pytest.raises(ValueError, match="表达式类型 date 与输出类型 string"):
        SemanticContract.model_validate(payload)
