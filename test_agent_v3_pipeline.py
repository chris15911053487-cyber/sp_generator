import pytest

from app.agent import nodes
from app.contracts.reference import ReferenceFactDesign
from app.contracts.semantic import SemanticDesign
from app.services.schema_binding_v3 import SchemaBindingError
from v3_test_helpers import catalog, contract


def test_agent_freezes_reference_before_generating_procedure(monkeypatch):
    import app.services.catalog_v3 as catalog_service
    import app.services.procedure_generator_v3 as procedure_service
    import app.services.reference_planner as reference_service
    import app.services.schema_binding_v3 as binding_service
    import app.services.validation_cases as case_service

    events = []

    class Artifact:
        def __init__(self, name):
            self.name = name

        def model_dump(self, **_kwargs):
            result = {"artifact": self.name}
            if self.name == "binding":
                result["catalog_fingerprint"] = "f" * 64
            return result

    monkeypatch.setattr(
        catalog_service, "capture_catalog_snapshot", lambda: catalog()
    )
    monkeypatch.setattr(
        nodes,
        "_generate_schema_binding_proposal_v3",
        lambda *_args: Artifact("proposal"),
    )
    monkeypatch.setattr(
        binding_service,
        "build_schema_binding",
        lambda *_args: Artifact("binding"),
    )
    monkeypatch.setattr(
        case_service, "discover_validation_cases", lambda *_args: []
    )
    monkeypatch.setattr(
        nodes,
        "_generate_reference_fact_designs_v3",
        lambda *_args: [
            ReferenceFactDesign(
                fact_id="invoice_income",
                meaning="发票收入事实",
                actual_projection=["InvoiceId", "Amount"],
            )
        ],
    )
    monkeypatch.setattr(
        nodes,
        "_generate_relational_plan_v3",
        lambda _llm, role, *_args, **_kwargs: (
            events.append(f"plan:{role}") or Artifact(role)
        ),
    )
    monkeypatch.setattr(
        reference_service,
        "freeze_reference_bundle",
        lambda *_args, **_kwargs: (
            events.append("freeze_reference") or Artifact("reference")
        ),
    )
    monkeypatch.setattr(
        procedure_service,
        "generate_procedure_candidate",
        lambda *_args, **_kwargs: (
            events.append("generate_procedure") or Artifact("procedure")
        ),
    )

    result = nodes._generate_node_v3(
        {"session_id": "test"},
        SemanticDesign(
            design_version="v3-design",
            decision_hash="v3-design",
            contracts=[contract()],
        ),
        object(),
        None,
    )

    assert result["status"] == "candidate_generated"
    assert events == [
        "plan:reference fact invoice_income: 发票收入事实",
        "freeze_reference",
        "plan:procedure",
        "generate_procedure",
    ]


def test_legacy_query_spec_is_rejected_instead_of_compatibly_converted(
    monkeypatch,
):
    monkeypatch.setattr(nodes, "_get_llm", lambda: object())
    monkeypatch.setattr(nodes, "_get_writer", lambda _config=None: None)
    result = nodes.generate_node(
        {
            "session_id": "legacy",
            "query_spec": {
                "contract_version": 2,
                "design_version": "old",
                "procedures": [],
            },
        }
    )
    assert result["status"] == "generate_failed"
    assert "SemanticDesign" in result["error"] or "validation" in result["error"]


def test_real_schema_ambiguity_is_not_retried_as_an_automatic_repair(
    monkeypatch,
):
    import app.services.catalog_v3 as catalog_service

    calls = []
    monkeypatch.setattr(
        catalog_service, "capture_catalog_snapshot", lambda: catalog()
    )

    def raise_ambiguity(*_args):
        calls.append("binding")
        raise SchemaBindingError(
            "SCHEMA_OBJECT_AMBIGUOUS",
            "需要用户选择",
            evidence={"ambiguities": []},
        )

    monkeypatch.setattr(
        nodes, "_generate_schema_binding_proposal_v3", raise_ambiguity
    )

    with pytest.raises(SchemaBindingError, match="需要用户选择"):
        nodes._generate_node_v3(
            {"session_id": "test"},
            SemanticDesign(
                design_version="v3-design",
                decision_hash="v3-design",
                contracts=[contract()],
            ),
            object(),
            None,
        )

    assert calls == ["binding"]


def test_fact_derived_formulas_are_promoted_to_single_result_binding_source():
    draft = {
        "contracts": [{
            "facts": [{"id": "business_fact"}],
            "result_bindings": [{
                "output_id": "amount",
                "expression": {
                    "kind": "fact_value",
                    "fact_value": {
                        "fact_id": "business_fact",
                        "value_id": "amount",
                    },
                },
            }],
            "derived_fields": [{
                "output_id": "difference",
                "expression": {
                    "kind": "binary",
                    "operator": "-",
                    "args": [
                        {"kind": "output", "output_id": "amount"},
                        {"kind": "literal", "value": 1},
                    ],
                },
            }],
        }]
    }

    nodes._promote_fact_derived_bindings_v3(draft)

    contract = draft["contracts"][0]
    assert contract["derived_fields"] == []
    assert contract["result_bindings"][1] == {
        "output_id": "difference",
        "expression": {
            "kind": "binary",
            "operator": "-",
            "args": [
                {"kind": "output", "output_id": "amount"},
                {"kind": "literal", "value": 1},
            ],
        },
    }


def test_fact_derived_formula_replaces_only_a_self_reference_placeholder():
    formula = {
        "kind": "binary",
        "operator": "-",
        "args": [
            {"kind": "output", "output_id": "left_amount"},
            {"kind": "output", "output_id": "right_amount"},
        ],
    }
    draft = {
        "contracts": [{
            "facts": [{"id": "business_fact"}],
            "result_bindings": [{
                "output_id": "difference",
                "expression": {
                    "kind": "output",
                    "output_id": "difference",
                },
            }],
            "derived_fields": [{
                "output_id": "difference",
                "expression": formula,
            }],
        }]
    }

    nodes._promote_fact_derived_bindings_v3(draft)

    contract = draft["contracts"][0]
    assert contract["derived_fields"] == []
    assert contract["result_bindings"][0]["expression"] == formula


def test_fact_derived_formula_does_not_overwrite_a_conflicting_formula():
    draft = {
        "contracts": [{
            "facts": [{"id": "business_fact"}],
            "result_bindings": [{
                "output_id": "difference",
                "expression": {"kind": "literal", "value": 1},
            }],
            "derived_fields": [{
                "output_id": "difference",
                "expression": {"kind": "literal", "value": 2},
            }],
        }]
    }

    nodes._promote_fact_derived_bindings_v3(draft)

    contract = draft["contracts"][0]
    assert contract["result_bindings"][0]["expression"]["value"] == 1
    assert len(contract["derived_fields"]) == 1


def test_fact_expression_roles_normalize_parameter_and_null_predicate():
    draft = {
        "contracts": [{
            "facts": [{"id": "business_fact"}],
            "parameters": [{"id": "tolerance_amount"}],
            "outputs": [{"id": "status"}],
            "result_bindings": [{
                "output_id": "status",
                "expression": {
                    "kind": "case",
                    "cases": [{
                        "when": {
                            "kind": "binary",
                            "operator": "=",
                            "args": [
                                {
                                    "kind": "fact_value",
                                    "fact_value": {
                                        "fact_id": "business_fact",
                                        "value_id": "amount",
                                    },
                                },
                                {"kind": "literal", "value": None},
                            ],
                        },
                        "then": {
                            "kind": "output",
                            "output_id": "tolerance_amount",
                        },
                    }],
                },
            }],
        }]
    }

    nodes._normalize_fact_expression_roles_v3(draft)

    expression = draft["contracts"][0]["result_bindings"][0]["expression"]
    assert expression["cases"][0]["when"]["kind"] == "unary"
    assert expression["cases"][0]["when"]["operator"] == "IS NULL"
    assert expression["cases"][0]["then"] == {
        "kind": "parameter",
        "parameter_id": "tolerance_amount",
    }


def test_fact_expression_roles_normalize_common_operator_aliases():
    draft = {
        "contracts": [{
            "facts": [{"id": "business_fact"}],
            "parameters": [],
            "outputs": [{"id": "difference"}],
            "result_bindings": [{
                "output_id": "difference",
                "expression": {
                    "kind": "function",
                    "operator": "SUBTRACT",
                    "args": [
                        {"kind": "literal", "value": 2},
                        {"kind": "literal", "value": 1},
                    ],
                },
            }],
            "result_filter": {
                "kind": "binary",
                "operator": "GREATER_THAN",
                "args": [
                    {"kind": "output", "output_id": "difference"},
                    {"kind": "literal", "value": 0},
                ],
            },
        }]
    }

    nodes._normalize_fact_expression_roles_v3(draft)

    contract = draft["contracts"][0]
    assert contract["result_bindings"][0]["expression"]["kind"] == "binary"
    assert contract["result_bindings"][0]["expression"]["operator"] == "-"
    assert contract["result_filter"]["operator"] == ">"

    contract["result_filter"]["operator"] = "ne"
    nodes._normalize_fact_expression_roles_v3(draft)
    assert contract["result_filter"]["operator"] == "<>"


def test_only_redundant_parenthesized_physical_annotation_is_removed():
    value = {
        "contracts": [{
            "entities": [{
                "id": "account",
                "meaning": "科目主数据，通过科目类型(ActType)区分收入科目",
            }],
            "purpose": "保留全部业务含义",
        }]
    }

    normalized = nodes._strip_redundant_physical_annotations_v3(value)

    assert normalized["contracts"][0]["entities"][0]["meaning"] == (
        "科目主数据，通过科目类型区分收入科目"
    )
    assert normalized["contracts"][0]["purpose"] == "保留全部业务含义"


def test_known_physical_term_in_meaning_is_replaced_by_business_term():
    value = {
        "contracts": [{
            "fact_joins": [{
                "meaning": "发票与凭证按 TransId 匹配",
            }],
        }]
    }

    normalized = nodes._strip_redundant_physical_annotations_v3(value)

    assert normalized["contracts"][0]["fact_joins"][0]["meaning"] == (
        "发票与凭证按 内部会计交易标识 匹配"
    )


def test_financial_reconciliation_cannot_silently_omit_amount_assumptions():
    assumptions = nodes._ensure_reconciliation_assumptions(
        "统计销售收入并与财务凭证进行比对",
        [{
            "key": "date_basis",
            "title": "日期口径",
            "value": "按双方过账日期",
            "reason": "避免跨期",
        }],
    )

    keys = {item["key"] for item in assumptions}
    assert {
        "currency_basis",
        "revenue_amount_basis",
        "journal_sign_basis",
        "cancellation_reversal_policy",
    }.issubset(keys)


def test_non_reconciliation_request_does_not_gain_financial_assumptions():
    assumptions = nodes._ensure_reconciliation_assumptions(
        "查询应收发票明细",
        [],
    )

    assert assumptions == []


def test_confirmed_amount_decision_is_not_overridden_by_default_assumption():
    assumptions = nodes._ensure_reconciliation_assumptions(
        "统计销售收入并与财务凭证进行比对",
        [],
        {"amount_basis"},
    )

    assert "revenue_amount_basis" not in {
        item["key"] for item in assumptions
    }


def test_inclusive_date_between_is_canonicalized_to_full_day_range():
    draft = {
        "contracts": [{
            "parameters": [
                {
                    "id": "from_date",
                    "logical_type": "date",
                    "boundary": "inclusive",
                },
                {
                    "id": "to_date",
                    "logical_type": "date",
                    "boundary": "inclusive_full_day",
                },
            ],
            "filters": [{
                "id": "date_range",
                "operator": "between",
                "parameter_ids": ["from_date", "to_date"],
            }],
        }]
    }

    nodes._canonicalize_full_day_boundaries(draft)

    assert draft["contracts"][0]["filters"][0]["operator"] == (
        "full_day_range"
    )


def test_known_physical_output_names_are_canonicalized_to_business_names():
    draft = {
        "contracts": [{
            "outputs": [
                {"id": "customer_code", "name": "CardCode"},
                {"id": "document_date", "name": "DocDate"},
                {"id": "account_code", "name": "AcctCode"},
            ],
        }]
    }

    nodes._canonicalize_business_output_names_v3(draft)

    assert [item["name"] for item in draft["contracts"][0]["outputs"]] == [
        "CustomerCode",
        "DocumentDate",
        "AccountCode",
    ]
